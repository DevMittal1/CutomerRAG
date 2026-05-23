# CustomerRAG End-to-End Architecture

This document explains the full system architecture implemented in this repository, from user authentication through vector synchronization into Qdrant.

## 1. System Goal

CustomerRAG is a multi-service backend for document ingestion and retrieval preparation. Its primary job is to:

1. accept authenticated document uploads
2. store upload metadata and lifecycle state
3. process uploaded files through one of two parsing paths
4. generate text chunks
5. submit chunks for embedding generation
6. sync completed vectors into Qdrant

The system is designed around asynchronous processing, idempotent state transitions, and worker-level horizontal scalability.

## 2. High-Level Topology

```text
Client
  |
  v
FastAPI API
  |
  | create document record + presigned upload URL
  v
S3
  |
  v
S3 Event -> SQS
  |
  v
S3 Ingestion Worker
  |------------------------------|
  |                              |
  v                              v
Redis Stream                 Landing AI Submit
  |                              |
  v                              v
      Unified Chunk Worker (Local/External)
  |                              |
  | store chunks                 | store chunks
  | submit Gemini job            | submit Gemini job
  v                              v
MongoDB documents + chunks + embedding job metadata
  |
  v
Embedding Sync Worker
  |
  v
Gemini Batch Embeddings
  |
  v
Qdrant
  |
  v
RAG Chat API
  |
  v
MongoDB rag_evaluation_traces
  |
  v
RAGAS Evaluation Worker
```

## 3. Repository Service Map

### `apps/api`

Primary HTTP entrypoint. Responsibilities:

- application startup and MongoDB connection lifecycle
- JWT-based authentication
- request validation
- request ID propagation
- rate limiting
- S3 pre-signed upload session generation
- upload confirmation
- document listing

Main files:

- [apps/api/app/main.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/api/app/main.py)
- [apps/api/app/routers/auth.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/api/app/routers/auth.py)
- [apps/api/app/routers/documents.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/api/app/routers/documents.py)
- [apps/api/app/db.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/api/app/db.py)

### `apps/s3_ingestion`

I/O-bound routing worker connected to SQS. Responsibilities:

- receive S3 event notifications from SQS
- extend message visibility while processing
- claim document state in MongoDB
- decide internal vs external processing path
- enqueue Redis Stream work for local chunking
- download file and submit it to Landing AI for external parsing

Main files:

- [apps/s3_ingestion/main.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/s3_ingestion/main.py)
- [apps/s3_ingestion/app/processor.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/s3_ingestion/app/processor.py)

### `apps/chunk_worker`

Unified chunking orchestrator combining both the internal LlamaIndex Redis consumer and the external Landing AI poller. Responsibilities:

- **Local Mode**: consume Redis Stream, split content with `HierarchicalNodeParser`, store chunks.
- **External Mode**: claim and poll Landing AI jobs, process terminal states, normalize chunks into MongoDB.
- submit Gemini embedding batch jobs (shared).

Main files:

- `apps/chunk_worker/main.py`: Entry point resolving `CHUNK_WORKER_MODE` (`local`, `external`, `both`)
- `apps/chunk_worker/app/providers/local.py`
- `apps/chunk_worker/app/providers/external.py`
- `apps/chunk_worker/app/services/embeddings.py`

### `packages/*` (Shared Packages)

Reusable internal libraries decoupled from app-specific logic:
- `packages/contracts`: Standardized Pydantic schemas (ingestion, retrieval).
- `packages/observability`: Correlation ID tracking and structured worker loggers.
- `packages/queue`: Redis connection pools and HTTP retry strategies.

### `apps/embedding_sync_worker`

Final synchronization worker from Gemini to Qdrant. Responsibilities:

- claim documents with in-flight embedding jobs
- poll Gemini batch job state
- handle active, success, and failed states
- map embedding responses back to stored chunks
- build Qdrant points and payloads
- upsert vectors in batches
- optionally create the Qdrant collection

Main files:

- [apps/embedding_sync_worker/main.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/embedding_sync_worker/main.py)
- [apps/embedding_sync_worker/app/poller.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/embedding_sync_worker/app/poller.py)
- [apps/embedding_sync_worker/app/config.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/embedding_sync_worker/app/config.py)

### `apps/ragas_eval_worker`

Dedicated offline evaluator for retrieval traces. Responsibilities:

- lease pending evaluation traces from MongoDB
- run RAGAS metrics outside the request path
- compare `before_rerank` and `after_rerank` retrieval variants
- write scores and failures back to MongoDB

Main files:

- [apps/ragas_eval_worker/main.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/ragas_eval_worker/main.py)
- [apps/ragas_eval_worker/app/poller.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/ragas_eval_worker/app/poller.py)
- [apps/ragas_eval_worker/app/config.py](/home/hf/Documents/rag-prod/CutomerRAG/apps/ragas_eval_worker/app/config.py)

## 4. End-to-End Request And Processing Flow

### 4.1 Authentication

The API supports:

- `POST /auth/signup`
- `POST /auth/signin`
- `GET /auth/me`
- `POST /auth/token`

User records are stored in MongoDB with:

- unique email
- password hash
- timestamps

JWTs are used for authenticated API access.

### 4.2 Upload Session Creation

The client calls `POST /documents/presigned-url` with:

- filename
- MIME type
- file size
- route hint for external parsing

The API then:

1. sanitizes and validates the request
2. generates a unique S3 object key under `uploads/{user_id}/...`
3. creates a pre-signed `put_object` URL
4. inserts a MongoDB `documents` record with status `pending`

Stored metadata includes:

- `filename`
- `file_key`
- `bucket`
- `status`
- `content_type`
- `file_size_bytes`
- `user_id`
- `created_at`
- `updated_at`
- `use_external_parser`

### 4.3 Direct Upload To S3

The client uploads the file directly to S3 using the returned pre-signed URL. This avoids routing binary uploads through the API server.

### 4.4 Upload Confirmation

The client calls `POST /documents/{document_id}/confirm`.

The API:

1. validates the document ID
2. checks ownership
3. transitions `pending -> uploaded`

At this point, upload success is recorded, but parsing and chunking are not yet complete.

### 4.5 S3 Event Delivery

Once the object exists in S3, an S3 event is expected to reach the ingestion worker through SQS.

The ingestion worker:

- long-polls SQS
- processes multiple messages concurrently
- starts a visibility-timeout heartbeat
- parses the S3 event payload

### 4.6 Document Claiming And Route Decision

The ingestion processor atomically claims a document record by updating records where status is in:

- `pending`
- `uploaded`

The claim transition sets:

- `status = processing`
- `last_sqs_message_id`
- `updated_at`

If a record is already owned or terminal, the event is skipped.

Then the worker decides between two routes:

- internal route when `use_external_parser` is false
- external route when `use_external_parser` is true

### 4.7 Internal Chunking Route

For internal processing:

1. an event is added to Redis Stream `rag:chunks`
2. document status becomes `queued_for_chunking`
3. local chunk worker consumes the stream
4. worker claims the document with status `chunking`
5. worker loads the file from S3 using `S3Reader`
6. `HierarchicalNodeParser` creates chunk nodes
7. prior document chunks are deleted
8. new chunks are inserted into MongoDB
9. Gemini embedding batch job is submitted inline
10. document is marked `completed` with embedding metadata attached

Chunk worker document fields include:

- `chunk_owner`
- `chunk_claimed_at`
- `current_chunk_stream_id`
- `chunk_attempts`
- `chunk_count`

Failure path:

- status becomes `chunk_failed`
- `chunk_error` is stored

Recovery path:

- Redis pending messages can be reclaimed with `XAUTOCLAIM`
- stale chunk claims can be re-owned if old enough

### 4.8 External Parsing Route

For external processing:

1. ingestion worker downloads the file from S3
2. file bytes are submitted to Landing AI
3. document is updated to `landing_ai_pending`
4. `landing_ai_job_id` is stored
5. external poller periodically checks job status

The external poller uses:

- reusable `httpx.AsyncClient`
- adaptive poll delay
- transient error handling
- job timeout checks
- distributed worker claiming

On success:

1. old chunks for the job are deleted
2. new chunks are inserted in batches
3. chunk metadata such as page and bounding box is preserved
4. Gemini embedding batch job is submitted
5. document status becomes `completed`

On terminal failure:

- status becomes `failed`
- `error` is recorded

## 5. Embedding Pipeline

Both parsing routes feed the same embedding lifecycle model.

### 5.1 Gemini Submission

Each chunk becomes an embedding request that includes:

- output dimensionality
- text payload in the form `task: search_document | text: ...`
- metadata such as document ID, file key, chunk index, and optional chunk/job IDs

The system submits embeddings as Gemini batch jobs, not one-by-one synchronous calls.

Document embedding fields include:

- `embedding_job_id`
- `embedding_status`
- `embedding_model`
- `embedding_poll_attempts`
- `embedding_submitted_at`
- `embedding_next_poll_at`
- `embedding_updated_at`
- `embedding_error`

### 5.2 Gemini Polling

The embedding sync worker claims documents whose `embedding_status` is:

- `submitted`
- `polling`

It also respects:

- `embedding_next_poll_at`
- `embedding_lock_expires_at`
- `EMBEDDING_POLL_CLAIM_LIMIT`
- `EMBEDDING_POLL_CONCURRENCY`

Gemini states are treated as:

- active: queue, pending, running, cancelling, paused, updating
- failed: failed, cancelled, expired, partially succeeded
- succeeded: vectors are ready for sync

### 5.3 Result Extraction

The sync worker supports two Gemini output forms:

- inline embedding responses
- output file download

It maps each embedding back to a MongoDB chunk using metadata, especially `chunk_index`.

### 5.4 Qdrant Upsert

Each chunk becomes a Qdrant point containing:

- point ID from `chunk_id` or `document_id:chunk_index`
- dense vector
- optional sparse vector
- payload metadata

Payload includes:

- `document_id`
- `file_key`
- `chunk_index`
- `chunk_id`
- `content`
- `metadata`
- `source_url`
- `page`
- `bounding_box`
- `landing_ai_job_id`
- `embedding_job_id`

Upserts are batched according to `QDRANT_UPSERT_BATCH_SIZE`.

On success, document fields are updated with:

- `embedding_status = completed`
- `embedding_synced_count`
- `embedding_completed_at`
- `embedding_output_file`

The worker also deletes Gemini output files after a successful sync when applicable.

## 6. Data Model

### `users` collection

Expected fields:

- `_id`
- `email`
- `password_hash`
- `created_at`
- `updated_at`

Indexes:

- unique `email`

### `documents` collection

Expected core fields:

- `_id`
- `filename`
- `file_key`
- `bucket`
- `status`
- `content_type`
- `file_size_bytes`
- `user_id`
- `created_at`
- `updated_at`
- `use_external_parser`

Additional routing and processing fields may include:

- `last_sqs_message_id`
- `chunk_stream_id`
- `chunk_owner`
- `chunk_claimed_at`
- `chunk_attempts`
- `chunk_count`
- `current_chunk_stream_id`
- `landing_ai_job_id`
- `poll_attempts`
- `next_poll_at`
- `locked_by`
- `lock_expires_at`
- `error`

Additional embedding fields may include:

- `embedding_job_id`
- `embedding_status`
- `embedding_model`
- `embedding_poll_attempts`
- `embedding_submitted_at`
- `embedding_next_poll_at`
- `embedding_updated_at`
- `embedding_remote_state`
- `embedding_synced_count`
- `embedding_completed_at`
- `embedding_output_file`
- `embedding_locked_by`
- `embedding_lock_expires_at`
- `embedding_error`

Indexes defined or ensured across the codebase include:

- unique `file_key`
- `(user_id, created_at)`
- `(status, updated_at)`
- `(embedding_status, embedding_next_poll_at)`
- `embedding_lock_expires_at`

### `chunks` collection

Internal chunking records may include:

- `document_id`
- `file_key`
- `chunk_index`
- `chunk_id`
- `content`
- `metadata`
- `created_at`

External parsing records additionally include:

- `job_id`
- `source_url`
- `chunk_type`
- `page`
- `bounding_box`

Indexes include:

- unique `(document_id, chunk_index)`
- unique `(job_id, chunk_index)` for external path safety

## 7. State Machines

### Document lifecycle

```text
pending
  -> uploaded
  -> processing
  -> queued_for_chunking
  -> chunking
  -> completed

pending
  -> uploaded
  -> processing
  -> landing_ai_pending
  -> completed

failure branches:
processing -> failed
chunking -> chunk_failed
landing_ai_pending -> failed
```

### Embedding lifecycle

```text
submitted
  -> polling
  -> completed

submitted
  -> polling
  -> failed

optional:
skipped
```

## 8. Reliability And Scaling Patterns

The codebase uses several production-oriented patterns:

- MongoDB `find_one_and_update` for atomic claims
- unique indexes for idempotency
- Redis Streams consumer groups for parallel chunking
- SQS visibility heartbeats for long-running ingestion tasks
- reclaim loops for abandoned Redis pending work
- worker lock expiration for distributed pollers
- adaptive retry and jitter for external API polling
- batch writes to MongoDB and Qdrant
- bounded concurrency via semaphores
- graceful shutdown through signal handlers

## 9. Configuration Surface

### Shared core dependencies

- `MONGODB_URI`
- `MONGODB_DB_NAME`
- `GOOGLE_API_KEY`
- `QDRANT_URL`
- `QDRANT_API_KEY`
- AWS credentials and region

### API-specific

- `JWT_SECRET_KEY`
- `ACCESS_TOKEN_EXPIRE_MINUTES`
- `CORS_ORIGINS`
- `RATE_LIMIT_REQUESTS`
- `RATE_LIMIT_WINDOW_SECONDS`

### S3 ingestion

- `AWS_SQS_QUEUE_URL`
- `AWS_SQS_WAIT_TIME_SECONDS`
- `MAX_CONCURRENT_TASKS`
- `REDIS_CHUNK_STREAM`

### Local chunking

- `REDIS_URI`
- `REDIS_CHUNK_CONSUMER_GROUP`
- `CHUNK_SIZES`
- `MAX_CONCURRENT_TASKS`

### External parsing

- `LANDING_AI_API_KEY`
- `LANDING_AI_BASE_URL`

### Embedding sync

- `QDRANT_COLLECTION_NAME`
- `QDRANT_VECTOR_SIZE`
- `QDRANT_DISTANCE`
- `QDRANT_DENSE_VECTOR_NAME`
- `QDRANT_SPARSE_VECTOR_NAME`
- `EMBEDDING_POLL_INTERVAL_SECONDS`
- `EMBEDDING_POLL_CLAIM_LIMIT`
- `EMBEDDING_POLL_CONCURRENCY`

## 10. Startup Order

Recommended logical startup order:

1. MongoDB
2. Redis
3. Qdrant
4. S3 + SQS infrastructure
5. FastAPI API
6. S3 ingestion worker
7. local chunk worker
8. external chunk poller
9. embedding sync worker

## 11. Operational Observations

This repository is centered on indexing and vector readiness, not on a final retrieval API or chat frontend. The document record in MongoDB acts as the system-of-record state machine coordinating the rest of the pipeline.

The architecture is strongest in these areas:

- async ingestion boundaries
- ownership and idempotency patterns
- separated worker concerns
- explicit vector sync stage

It is a solid base for:

- enterprise document indexing
- search ingestion systems
- internal knowledge base pipelines
- production-style RAG preprocessing

## 12. Related Files

- [README.md](./README.md)
- [s3upload.md](./s3upload.md)
- [k6/load_test.js](./k6/load_test.js)
- [k6/stress_test.js](./k6/stress_test.js)
