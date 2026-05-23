# Potential Bugs and Production Risks

This document is a production-readiness review of the current CustomerRAG codebase. It focuses on API correctness, worker scalability, queue safety, and retrieval/index consistency.

## Review Scope

Reviewed components:

- `apps/api`
- `apps/s3_ingestion`
- `apps/chunk_worker`
- `apps/embedding_sync_worker`
- `apps/ragas_eval_worker`

## Priority Summary

| Priority | Area | Finding | Primary Risk |
|---|---|---|---|
| P0 | API / Upload | `POST /documents/presigned-url` uses `payload.use_external_parser`, but the request schema does not define it | Immediate 500s on upload session creation |
| P0 | API / Upload | `regenerate-upload-url` can reopen an already-uploaded or in-flight document key | Overwrites in-flight S3 objects and corrupts processing state |
| P1 | API / Rate limiting | Rate limiting is enforced twice on many routes | Effective quota is cut in half and Redis load doubles |
| P1 | Workers / SQS | Ingestion worker creates tasks before concurrency is actually bounded | Task backlog and memory growth under sustained queue pressure |
| P1 | Workers / Redis Streams | Chunk reclaim loop always restarts `XAUTOCLAIM` from `0-0` | Poor reclaim scalability as pending backlog grows |
| P1 | External parser worker | Landing AI timeout is measured from document creation time, not parser submission time | False job failures during upload delays or queue backlog |
| P2 | Retrieval / Index sync | Re-chunking removes Mongo chunks but never deletes stale Qdrant points | Retrieval can return deleted or obsolete content |
| P2 | API / Security / Rate limiting | Client-controlled `X-Forwarded-For` is trusted directly, while Uvicorn also trusts all forwarded IPs | IP spoofing can bypass or distort rate limits |

## Detailed Findings

### P0. `POST /documents/presigned-url` references a field that the request model does not expose

**Evidence**

- [apps/api/app/schemas.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/api/app/schemas.py:86) defines `PresignedUrlRequest` with `filename`, `content_type`, and `file_size_bytes` only.
- [apps/api/app/routers/documents.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/api/app/routers/documents.py:92) writes `payload.use_external_parser` into Mongo.

**Why this is a bug**

The router expects `use_external_parser`, but the request schema never declares it. On a normal FastAPI/Pydantic request, `payload.use_external_parser` will raise an attribute error and turn upload session creation into a 500.

**Production impact**

- Breaks the first step of the document ingestion flow.
- Prevents both internal and external parsing routes from being selected intentionally.
- Makes the upload API unreliable even before workers are involved.

**Recommended fix**

- Add `use_external_parser: bool = False` to `PresignedUrlRequest`.
- Add the same field to `DocumentResponse` if clients are expected to see it back.
- Add a request test for both `true` and `false` values.

---

### P0. Upload URL regeneration is allowed for in-flight documents, not just pending ones

**Evidence**

- [apps/api/app/routers/documents.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/api/app/routers/documents.py:298) says regeneration should only be allowed for pending uploads.
- The actual guard at [apps/api/app/routers/documents.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/api/app/routers/documents.py:299) only blocks `status == "completed"`.

**Why this is a bug**

Any document in `uploaded`, `processing`, `queued_for_chunking`, `chunking`, or `landing_ai_pending` can still receive a fresh PUT URL for the same S3 key. That allows a client to overwrite the object while downstream workers may already be reading or parsing it.

**Production impact**

- Corrupts ingestion determinism.
- Can cause a parser to read content different from what the metadata record represents.
- Makes retries and incident debugging extremely difficult because the object may change mid-flight.

**Recommended fix**

- Only allow regeneration when `status == "pending"`.
- Reject regeneration for any state after upload confirmation.
- Consider versioned object keys if overwrites must ever be supported.

---

### P1. Rate limiting is applied twice for many API routes

**Evidence**

- Global middleware rate limits every non-exempt request at [apps/api/app/main.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/api/app/main.py:93).
- Route-level dependencies also rate limit again at [apps/api/app/rate_limiter.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/api/app/rate_limiter.py:70).
- Example routes with duplicate enforcement include:
  - [apps/api/app/routers/auth.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/api/app/routers/auth.py:22)
  - [apps/api/app/routers/documents.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/api/app/routers/documents.py:26)
  - [apps/api/app/routers/chat.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/api/app/routers/chat.py:21)

**Why this is a bug**

For those routes, a single request consumes two rate-limit checks and two Redis write paths. In practice, clients hit 429s earlier than configured, and the system does extra work to reject them.

**Production impact**

- Effective rate limits are lower than advertised.
- Redis load is amplified for the hottest endpoints.
- Operators will see confusing mismatch between configured quotas and observed throttling.

**Recommended fix**

- Choose one enforcement layer only:
  - global middleware, or
  - per-route dependency.
- Keep header-setting logic in one place.
- Add an integration test proving one request consumes one quota unit.

---

### P1. The S3 ingestion worker can build an unbounded pending task backlog

**Evidence**

- The worker receives messages continuously in [apps/s3_ingestion/main.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/s3_ingestion/main.py:56).
- It creates a task for every message at [apps/s3_ingestion/main.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/s3_ingestion/main.py:65).
- Concurrency is only enforced inside `_process_message_wrapper()` with the semaphore at [apps/s3_ingestion/main.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/s3_ingestion/main.py:85).

**Why this is a bug**

The semaphore limits active processing, but not task creation. Under sustained queue load, the event loop can accumulate a large set of pending tasks waiting for the semaphore, each still holding message state and callback overhead.

**Production impact**

- Memory growth during traffic spikes.
- Less predictable shutdown because many queued tasks must drain.
- Higher pressure on Python scheduling before useful work even starts.

**Recommended fix**

- Stop polling when `len(active_tasks)` reaches the configured concurrency limit.
- Or move message handling to a bounded worker queue and let only a fixed number of consumer coroutines exist.
- Consider using SQS batch size based on available slots, not just static config.

---

### P1. Redis Stream reclaim always starts from `0-0`, which does not scale

**Evidence**

- The reclaim loop calls `XAUTOCLAIM` with `start_id="0-0"` every cycle at [apps/chunk_worker/main.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/chunk_worker/main.py:114).
- The returned cursor is ignored at [apps/chunk_worker/main.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/chunk_worker/main.py:122).

**Why this is a bug**

`XAUTOCLAIM` is designed to return the next cursor so large pending-entry lists can be scanned incrementally. Restarting from `0-0` every reclaim pass causes repeated rescans of the same backlog.

**Production impact**

- Reclaim cost grows unnecessarily with stream backlog size.
- Idle workers waste Redis CPU rescanning old pending entries.
- Horizontal scale gets worse exactly when the system is already under pressure.

**Recommended fix**

- Persist and reuse the `next_start_id` returned by `XAUTOCLAIM`.
- Reset the cursor only after a full pass completes.
- Add metrics for reclaimed count versus reclaim scan cost.

---

### P1. Landing AI timeout starts from document creation, not parser submission

**Evidence**

- External parser timeout is computed from `created_at` at [apps/embedding_sync_worker/app/poller.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/embedding_sync_worker/app/poller.py:273).
- That timeout is enforced at [apps/embedding_sync_worker/app/poller.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/embedding_sync_worker/app/poller.py:345).
- Parser submission time is recorded later in the flow, not at document creation, in [apps/s3_ingestion/app/processor.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/s3_ingestion/app/processor.py:196).

**Why this is a bug**

The one-hour timeout includes time spent waiting for user upload confirmation, S3 event delivery, queue backlog, and worker scheduling. A perfectly healthy Landing AI job can be marked failed simply because the document record was created long before the parser actually received the file.

**Production impact**

- False failures on large uploads or busy systems.
- Unnecessary retries and manual operator intervention.
- Worse behavior during incidents, when queue delays are already elevated.

**Recommended fix**

- Track and use a dedicated `landing_ai_submitted_at` timestamp.
- Base timeout on actual submission or first poll time.
- Make the timeout configurable in settings rather than hardcoding 3600 seconds.

---

### P2. Re-chunking and re-embedding never delete stale vectors from Qdrant

**Evidence**

- Local chunk replacement deletes old Mongo chunks at [apps/chunk_worker/main.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/chunk_worker/main.py:242).
- External parser replacement deletes old job chunks at [apps/embedding_sync_worker/app/poller.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/embedding_sync_worker/app/poller.py:126).
- Embedding sync only upserts new Qdrant points at [apps/embedding_sync_worker/app/poller.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/embedding_sync_worker/app/poller.py:468) and [apps/embedding_sync_worker/app/poller.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/embedding_sync_worker/app/poller.py:508).

**Why this is a bug**

When chunk counts shrink, chunk IDs change, or a document is reprocessed, old vectors can remain in Qdrant even though the source chunks no longer exist in MongoDB. Retrieval can then surface stale content that no longer matches the document’s current state.

**Production impact**

- Incorrect retrieval answers.
- Mixed old/new chunk sets for the same document.
- Hard-to-debug “ghost context” during RAG evaluation.

**Recommended fix**

- Delete existing Qdrant points for a document before upserting its fresh chunk set.
- Or use a deterministic point ID namespace plus explicit diff-based deletion.
- Add a consistency check comparing Mongo chunk count and synced vector count.

---

### P2. Rate limiting trusts client-supplied forwarding headers too broadly

**Evidence**

- Middleware reads `X-Forwarded-For` directly at [apps/api/app/main.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/api/app/main.py:94).
- Route dependency does the same at [apps/api/app/rate_limiter.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/api/app/rate_limiter.py:76).
- Uvicorn is configured with `proxy_headers=True` and `forwarded_allow_ips="*"` at [apps/api/main.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/api/main.py:24).

**Why this is a bug**

The app trusts the forwarded client IP from any upstream source, then also re-parses the raw header itself. In deployments without strict proxy sanitization, clients can spoof IPs and distort rate-limit identity.

**Production impact**

- Rate-limit bypass or accidental throttling of the wrong identity.
- Security assumptions differ between environments.
- Harder incident response when logs and limits disagree about client origin.

**Recommended fix**

- Trust forwarded headers only from known proxy IPs.
- Prefer `request.client` after proxy normalization instead of reparsing raw headers in application code.
- Document the expected ingress/proxy topology explicitly.

## Suggested Fix Order

1. Fix the two P0 upload issues first.
2. Remove duplicate rate limiting and bound the ingestion worker task backlog.
3. Fix chunk reclaim cursor usage and external parser timeout anchoring.
4. Add Qdrant stale-vector cleanup before reprocessing becomes common in production.
5. Tighten forwarded-header trust before internet-facing rollout.

## Notes

- This report is intentionally biased toward production failure modes, not style concerns.
- Some findings may not surface in low-volume local testing but become high-impact under concurrency, retries, or long-running workloads.
