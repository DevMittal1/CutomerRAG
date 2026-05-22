# CustomerRAG

Production-style Retrieval-Augmented Generation (RAG) backend for secure document upload, asynchronous parsing, chunk generation, embedding orchestration, and Qdrant vector sync.

## Why This Project Exists

CustomerRAG is built for teams that want more than a demo chatbot. It provides a practical ingestion and indexing pipeline for documents, with:

- FastAPI-based auth and upload APIs
- Direct-to-S3 uploads using pre-signed URLs
- Async ingestion with SQS and Redis Streams
- Two document parsing paths: internal chunking or external parsing
- Gemini batch embedding submission
- Qdrant vector synchronization
- MongoDB-backed status tracking and idempotency

This repo is useful if you are searching for:

- `production rag backend`
- `fastapi rag api`
- `s3 presigned upload rag pipeline`
- `mongodb redis qdrant rag architecture`
- `async document ingestion for llm search`
- `gemini embeddings qdrant example`

## What It Does

The system accepts user uploads, tracks each document in MongoDB, routes the file through either an internal chunker or an external parser, submits chunk text for embeddings, then syncs the resulting vectors into Qdrant for retrieval use cases.

High-level flow:

1. User signs up or signs in.
2. API issues an S3 pre-signed upload URL and creates a `documents` record.
3. Client uploads file to S3 and confirms upload.
4. S3 event reaches the ingestion worker through SQS.
5. Ingestion worker decides:
   - internal chunking via Redis Streams
   - external parsing via Landing AI
6. Chunks are stored in MongoDB.
7. Gemini embedding batch jobs are submitted.
8. Embedding sync worker polls Gemini and upserts vectors into Qdrant.
9. Chat requests can emit Mongo-backed RAG evaluation traces for offline RAGAS scoring.


## File Structure

```

├── apps
│   ├── api
│   │   ├── app
│   │   ├── main.py
│   │   ├── pyproject.toml
│   │   └── uv.lock
│   ├── embedding_sync_worker
│   │   ├── app
│   │   ├── __init__.py
│   │   ├── main.py
│   │   └── pyproject.toml
│   ├── external_chunk_worker
│   │   ├── app
│   │   ├── main.py
│   │   └── pyproject.toml
│   ├── local_chunk_worker
│   │   ├── app
│   │   ├── __init__.py
│   │   ├── main.py
│   │   └── pyproject.toml
│   ├── ragas_eval_worker
│   │   ├── app
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── pyproject.toml
│   │   └── ragas_eval_worker.egg-info
│   └── s3_ingestion
│       ├── app
│       ├── __init__.py
│       ├── main.py
│       └── pyproject.toml
├── ARCHITECTURE.md
├── docs
│   ├── demo.docx
│   ├── Drafting-Pleadings-and-Conveyancing.pdf
│   └── s3upload.md
├── graphify-out
├── k6
│   ├── apis
│   │   ├── confirm.js
│   │   ├── me.js
│   │   ├── presigned_url.js
│   │   ├── signin.js
│   │   └── signup.js
│   ├── breakpoint_test.js
│   ├── config.js
│   ├── helpers.js
│   ├── load_test.js
│   └── stress_test.js
├── POTENTIAL_BUGS.md
├── README.md
├── test.sh
└── uv.lock
```

## Services

### `apps/api`

FastAPI application for:

- authentication
- rate limiting
- document upload session creation
- upload confirmation
- document listing

### `apps/s3_ingestion`

SQS-driven ingestion router that:

- listens for S3 object-created events
- claims document ownership in MongoDB
- routes work to internal or external processing

### `apps/local_chunk_worker`

Redis Stream consumer for internal chunking. It:

- reads documents from S3
- chunks them with LlamaIndex
- stores chunks in MongoDB
- submits embedding jobs to Gemini

### `apps/external_chunk_worker`

Landing AI poller that:

- monitors external parse jobs
- converts returned chunks into MongoDB records
- submits embedding jobs to Gemini

### `apps/embedding_sync_worker`

Gemini embedding poller that:

- tracks batch job completion
- downloads or reads inline embedding results
- maps embeddings back to chunks
- upserts vectors into Qdrant

### `apps/ragas_eval_worker`

Offline RAGAS evaluator that:

- claims pending chat traces from MongoDB
- evaluates `before_rerank` and `after_rerank` trace variants separately
- stores metric results back into MongoDB without slowing the live chat stream

## Core Infrastructure

- MongoDB for users, document state, and chunks
- Redis Streams for internal chunk work distribution
- AWS S3 for document storage
- AWS SQS for S3 event delivery
- Landing AI for external document parsing
- Gemini batch embeddings for vector generation
- Qdrant for vector search storage

## Document Lifecycle

Common statuses visible in the codebase include:

- `pending`
- `uploaded`
- `processing`
- `queued_for_chunking`
- `chunking`
- `landing_ai_pending`
- `completed`
- `failed`
- `chunk_failed`

Embedding-specific fields are tracked separately with values such as:

- `submitted`
- `polling`
- `completed`
- `failed`
- `skipped`

## Local Development Notes

Each app has its own `pyproject.toml`, and the workers use environment-based settings. The main dependencies implied by the code are:

- Python
- MongoDB
- Redis
- AWS credentials and S3/SQS setup
- Qdrant
- Gemini API key
- optional RAGAS worker with Gemini API key for offline evaluation
- optional Landing AI API key for external parsing

## API Highlights

Important routes include:

- `POST /auth/signup`
- `POST /auth/signin`
- `GET /auth/me`
- `POST /documents/presigned-url`
- `POST /documents/{document_id}/confirm`
- `GET /documents/`

## Load Testing

The `k6/` directory contains API and stress test scripts for:

- signup
- signin
- current user lookup
- pre-signed URL creation
- upload confirmation

## Deployment (Docker, Kubernetes, CI/CD)

Infrastructure lives under `infra/` with a root `Makefile` and GitHub Actions workflows.

- **Container registry:** [GitHub Container Registry (GHCR)](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry) — `ghcr.io/devmittal1/cutomerrag-*`
- **Build:** `make build` / `make build-api`
- **Push:** `make release` (requires `GITHUB_TOKEN`)
- **Kubernetes:** `make k8s-apply` (Kustomize overlay at `infra/kubernetes/overlays/prod`)

See [infra/README.md](./infra/README.md) for secrets, ingress, and cluster setup.

## Architecture Doc

The full end-to-end architecture README is here:

[ARCHITECTURE.md](./ARCHITECTURE.md)

## Good Fit For

- teams building secure document-ingestion backends
- engineers evaluating async RAG indexing pipelines
- developers looking for FastAPI + MongoDB + Redis + Qdrant patterns
- search and retrieval systems that need explicit document lifecycle tracking

## Current Shape Of The Repo

This repository is stronger on backend ingestion and indexing than on final chat/query UX. It is best understood as the ingestion and vectorization foundation for a larger RAG platform.
