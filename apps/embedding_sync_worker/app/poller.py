from __future__ import annotations

import asyncio
import io
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from google import genai
from google.genai.errors import APIError
from google.genai.types import BatchJob
from pymongo import AsyncMongoClient, ReturnDocument
from qdrant_client import AsyncQdrantClient, models

from .config import settings
from .utils.constants import RELEASE_EMBEDDING_LOCK_UPDATE
from .utils.logging import get_worker_logger

logger = get_worker_logger("worker.embedding_sync")

ACTIVE_STATES = {
    "JOB_STATE_QUEUED",
    "JOB_STATE_PENDING",
    "JOB_STATE_RUNNING",
    "JOB_STATE_CANCELLING",
    "JOB_STATE_PAUSED",
    "JOB_STATE_UPDATING",
}
FAILED_STATES = {
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED",
    "JOB_STATE_PARTIALLY_SUCCEEDED",
}


class GeminiEmbeddingPoller:
    """
    Polls Gemini embedding batch jobs and syncs completed vectors into Qdrant.
    """

    def __init__(self, db_client: AsyncMongoClient):
        self.db_client = db_client
        self.db = self.db_client[settings.MONGODB_DB_NAME]
        self.worker_id = str(uuid.uuid4())
        self.client = genai.Client(api_key=settings.GOOGLE_API_KEY).aio
        self.qdrant = AsyncQdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY,
        )

    async def close(self):
        await self.client.aclose()
        close_qdrant = getattr(self.qdrant, "close", None)
        if callable(close_qdrant):
            maybe_coro = close_qdrant()
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro
        else:
            await self.qdrant.close()

    async def ensure_indexes(self):
        await self.db[settings.COLL_DOCUMENTS].create_index(
            [("embedding_status", 1), ("embedding_next_poll_at", 1)]
        )
        await self.db[settings.COLL_DOCUMENTS].create_index(
            [("embedding_lock_expires_at", 1)]
        )
        await self.db[settings.COLL_CHUNKS].create_index(
            [("document_id", 1), ("chunk_index", 1)],
            unique=True,
        )
        await self.db[settings.COLL_CHUNKS].create_index(
            [("job_id", 1), ("chunk_index", 1)],
            unique=True,
        )

    async def ensure_qdrant_collection(self):
        collections = await self.qdrant.get_collections()
        if any(
            collection.name == settings.QDRANT_COLLECTION_NAME
            for collection in collections.collections
        ):
            return

        distance_name = settings.QDRANT_DISTANCE.upper()
        distance = getattr(models.Distance, distance_name, models.Distance.COSINE)
        dense_config: (
            models.VectorParams | dict[str, models.VectorParams] | dict[str, Any]
        )
        dense_params = models.VectorParams(
            size=settings.QDRANT_VECTOR_SIZE,
            distance=distance,
        )
        if settings.QDRANT_DENSE_VECTOR_NAME:
            dense_config = {settings.QDRANT_DENSE_VECTOR_NAME: dense_params}
        else:
            dense_config = dense_params

        sparse_config = None
        if settings.QDRANT_SPARSE_VECTOR_NAME:
            sparse_config = {
                settings.QDRANT_SPARSE_VECTOR_NAME: models.SparseVectorParams(
                    index=models.SparseIndexParams(
                        on_disk=settings.QDRANT_SPARSE_ON_DISK
                    )
                )
            }

        await self.qdrant.create_collection(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            vectors_config=dense_config,
            sparse_vectors_config=sparse_config,
        )
        logger.info(
            "Created Qdrant collection",
            extra={
                "collection": settings.QDRANT_COLLECTION_NAME,
                "dense_vector_name": settings.QDRANT_DENSE_VECTOR_NAME,
                "sparse_vector_name": settings.QDRANT_SPARSE_VECTOR_NAME,
            },
        )

    async def claim_documents(self, limit: int) -> list[dict]:
        now = datetime.now(timezone.utc)
        claimed_docs: list[dict] = []

        for _ in range(limit):
            doc = await self.db[settings.COLL_DOCUMENTS].find_one_and_update(
                {
                    "embedding_status": {"$in": ["submitted", "polling"]},
                    "$and": [
                        {
                            "$or": [
                                {"embedding_next_poll_at": {"$exists": False}},
                                {"embedding_next_poll_at": {"$lte": now}},
                            ]
                        },
                        {
                            "$or": [
                                {"embedding_lock_expires_at": {"$exists": False}},
                                {"embedding_lock_expires_at": {"$lte": now}},
                            ]
                        },
                    ],
                },
                {
                    "$set": {
                        "embedding_locked_by": self.worker_id,
                        "embedding_lock_expires_at": now
                        + timedelta(seconds=settings.EMBEDDING_LOCK_TIMEOUT_SECONDS),
                        "updated_at": now,
                    }
                },
                return_document=ReturnDocument.AFTER,
            )
            if not doc:
                break
            claimed_docs.append(doc)

        return claimed_docs

    def _job_timed_out(self, doc: dict, now: datetime) -> bool:
        started_at = (
            doc.get("embedding_submitted_at")
            or doc.get("embedding_updated_at")
            or doc.get("created_at")
        )
        if not started_at:
            return False
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        return (
            now - started_at
        ).total_seconds() > settings.EMBEDDING_JOB_TIMEOUT_SECONDS

    def _next_delay_seconds(self, attempts: int) -> float:
        base = settings.EMBEDDING_BASE_RETRY_SECONDS
        multiplier = settings.EMBEDDING_BACKOFF_MULTIPLIER ** max(attempts, 0)
        jitter = random.uniform(0, settings.EMBEDDING_JITTER_SECONDS)
        return min(settings.EMBEDDING_MAX_RETRY_SECONDS, base * multiplier + jitter)

    async def _mark_active(self, doc: dict, remote_state: str):
        attempts = doc.get("embedding_poll_attempts", 0) + 1
        delay = self._next_delay_seconds(attempts)
        now = datetime.now(timezone.utc)
        await self.db[settings.COLL_DOCUMENTS].update_one(
            {"_id": doc["_id"]},
            {
                "$set": {
                    "embedding_status": "polling",
                    "embedding_poll_attempts": attempts,
                    "embedding_remote_state": remote_state,
                    "embedding_next_poll_at": now + timedelta(seconds=delay),
                    "embedding_updated_at": now,
                },
                **RELEASE_EMBEDDING_LOCK_UPDATE,
            },
        )

    async def _mark_failed(self, doc: dict, reason: str, remote_state: str | None = None):
        now = datetime.now(timezone.utc)
        update_fields: dict[str, Any] = {
            "embedding_status": "failed",
            "embedding_error": reason,
            "embedding_updated_at": now,
        }
        if remote_state is not None:
            update_fields["embedding_remote_state"] = remote_state

        await self.db[settings.COLL_DOCUMENTS].update_one(
            {"_id": doc["_id"]},
            {
                "$set": update_fields,
                **RELEASE_EMBEDDING_LOCK_UPDATE,
            },
        )

    async def _mark_completed(
        self,
        doc: dict,
        remote_state: str,
        synced_count: int,
        output_file_name: str | None,
    ):
        now = datetime.now(timezone.utc)
        await self.db[settings.COLL_DOCUMENTS].update_one(
            {"_id": doc["_id"]},
            {
                "$set": {
                    "embedding_status": "completed",
                    "embedding_remote_state": remote_state,
                    "embedding_synced_count": synced_count,
                    "embedding_completed_at": now,
                    "embedding_updated_at": now,
                    "embedding_output_file": output_file_name,
                    "embedding_error": None,
                },
                "$unset": {
                    "embedding_next_poll_at": "",
                    "embedding_lock_expires_at": "",
                    "embedding_locked_by": "",
                },
            },
        )

    async def poll_single_document(self, doc: dict) -> str:
        job_name = doc.get("embedding_job_id")
        if not job_name:
            await self._mark_failed(doc, "Missing embedding_job_id on document.")
            return "failed"

        now = datetime.now(timezone.utc)
        if self._job_timed_out(doc, now):
            await self._mark_failed(
                doc,
                f"Embedding batch job timed out after {settings.EMBEDDING_JOB_TIMEOUT_SECONDS} seconds.",
            )
            return "timeout"

        try:
            batch_job = await self.client.batches.get(name=job_name)
            remote_state = batch_job.state.name if batch_job.state else "UNKNOWN"

            if remote_state == "JOB_STATE_SUCCEEDED":
                synced_count = await self.process_completed_job(doc, batch_job)
                await self._mark_completed(
                    doc,
                    remote_state=remote_state,
                    synced_count=synced_count,
                    output_file_name=getattr(batch_job.dest, "file_name", None),
                )
                return "completed"

            if remote_state in FAILED_STATES:
                error_message = self._extract_batch_error(batch_job)
                await self._mark_failed(doc, error_message, remote_state=remote_state)
                return "failed"

            if remote_state in ACTIVE_STATES:
                await self._mark_active(doc, remote_state=remote_state)
                return "transient"

            await self._mark_active(doc, remote_state=remote_state)
            return "transient"
        except APIError as exc:
            logger.warning(
                "Transient Gemini API error while polling embedding job",
                extra={"job_name": job_name, "error": str(exc)},
            )
            await self._mark_active(doc, remote_state="API_ERROR")
            return "transient"
        except Exception as exc:
            logger.exception(
                "Unexpected error while polling embedding job",
                extra={"job_name": job_name, "file_key": doc.get("file_key")},
            )
            await self._mark_active(doc, remote_state="UNEXPECTED_ERROR")
            return "transient"

    def _extract_batch_error(self, batch_job: BatchJob) -> str:
        error = getattr(batch_job, "error", None)
        if error is None:
            return "Embedding batch job failed without an explicit Gemini error."
        message = getattr(error, "message", None)
        code = getattr(error, "code", None)
        if message and code:
            return f"Gemini batch job failed with code {code}: {message}"
        if message:
            return message
        return "Embedding batch job failed."

    async def _load_chunk_lookup(self, doc: dict) -> dict[int, dict]:
        chunks = await self.db[settings.COLL_CHUNKS].find(
            {"document_id": doc["_id"]}
        ).sort("chunk_index", 1).to_list(length=None)
        if chunks:
            return {chunk["chunk_index"]: chunk for chunk in chunks}

        landing_ai_job_id = doc.get("landing_ai_job_id")
        if not landing_ai_job_id:
            return {}

        chunks = await self.db[settings.COLL_CHUNKS].find(
            {"job_id": landing_ai_job_id}
        ).sort("chunk_index", 1).to_list(length=None)
        return {chunk["chunk_index"]: chunk for chunk in chunks}

    def _coerce_chunk_index(self, value: Any, fallback: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    def _build_point_from_chunk(
        self,
        doc: dict,
        chunk: dict,
        vector: list[float],
    ) -> models.PointStruct:
        document_id = str(doc["_id"])
        chunk_index = int(chunk["chunk_index"])
        point_id = (
            chunk.get("chunk_id")
            or f"{document_id}:{chunk_index}"
        )
        payload = {
            "document_id": document_id,
            "file_key": doc.get("file_key"),
            "chunk_index": chunk_index,
            "chunk_id": chunk.get("chunk_id"),
            "content": chunk.get("content"),
            "metadata": chunk.get("metadata", {}),
            "source_url": chunk.get("source_url"),
            "page": chunk.get("page"),
            "bounding_box": chunk.get("bounding_box"),
            "landing_ai_job_id": doc.get("landing_ai_job_id"),
            "embedding_job_id": doc.get("embedding_job_id"),
        }
        point_vector = self._build_qdrant_vector(chunk=chunk, dense_vector=vector)
        return models.PointStruct(id=str(point_id), vector=point_vector, payload=payload)

    def _build_qdrant_vector(
        self,
        *,
        chunk: dict,
        dense_vector: list[float],
    ) -> list[float] | dict[str, Any]:
        if not settings.QDRANT_DENSE_VECTOR_NAME and not settings.QDRANT_SPARSE_VECTOR_NAME:
            return dense_vector

        vector_payload: dict[str, Any] = {}
        dense_vector_name = settings.QDRANT_DENSE_VECTOR_NAME or "default"
        vector_payload[dense_vector_name] = dense_vector

        sparse_vector_name = settings.QDRANT_SPARSE_VECTOR_NAME
        sparse_embedding = chunk.get("sparse_embedding")
        if sparse_vector_name and sparse_embedding:
            indices = sparse_embedding.get("indices") or []
            values = sparse_embedding.get("values") or []
            if len(indices) != len(values):
                raise ValueError(
                    f"Sparse embedding for chunk {chunk.get('chunk_index')} has mismatched indices and values lengths."
                )
            vector_payload[sparse_vector_name] = models.SparseVector(
                indices=indices,
                values=values,
            )

        return vector_payload

    def _extract_vector(self, payload: dict[str, Any]) -> list[float] | None:
        candidates = [
            payload.get("response", {}).get("embedding", {}).get("values"),
            payload.get("response", {}).get("embeddings", [{}])[0].get("values")
            if payload.get("response", {}).get("embeddings")
            else None,
            payload.get("embedding", {}).get("values"),
            payload.get("embeddings", [{}])[0].get("values")
            if payload.get("embeddings")
            else None,
        ]
        for candidate in candidates:
            if candidate:
                return candidate
        return None

    async def _points_from_inline_responses(
        self,
        doc: dict,
        responses: list[Any],
        chunk_lookup: dict[int, dict],
    ) -> list[models.PointStruct]:
        points: list[models.PointStruct] = []
        for idx, response in enumerate(responses):
            if getattr(response, "error", None) is not None:
                raise ValueError(
                    f"Embedding response error at index {idx}: {response.error}"
                )
            metadata = getattr(response, "metadata", {}) or {}
            chunk_index = self._coerce_chunk_index(metadata.get("chunk_index"), idx)
            chunk = chunk_lookup.get(chunk_index)
            if chunk is None:
                raise ValueError(
                    f"Could not map inline embedding response to chunk index {chunk_index}."
                )
            embedding = getattr(getattr(response, "response", None), "embedding", None)
            vector = getattr(embedding, "values", None)
            if not vector:
                raise ValueError(
                    f"Inline embedding response at chunk index {chunk_index} had no vector values."
                )
            points.append(self._build_point_from_chunk(doc, chunk, vector))
        return points

    async def _points_from_file_output(
        self,
        doc: dict,
        file_name: str,
        chunk_lookup: dict[int, dict],
    ) -> list[models.PointStruct]:
        data = await self.client.files.download(file=file_name)
        points: list[models.PointStruct] = []

        with io.TextIOWrapper(io.BytesIO(data), encoding="utf-8") as buffer:
            for line_number, line in enumerate(buffer):
                raw_line = line.strip()
                if not raw_line:
                    continue
                parsed = json.loads(raw_line)
                metadata = parsed.get("metadata", {}) or {}
                chunk_index = self._coerce_chunk_index(
                    metadata.get("chunk_index"),
                    line_number,
                )
                chunk = chunk_lookup.get(chunk_index)
                if chunk is None:
                    raise ValueError(
                        f"Could not map file embedding response to chunk index {chunk_index}."
                    )
                vector = self._extract_vector(parsed)
                if not vector:
                    raise ValueError(
                        f"Embedding output line {line_number} did not contain vector values."
                    )
                points.append(self._build_point_from_chunk(doc, chunk, vector))

        return points

    async def _upsert_points(self, points: list[models.PointStruct]):
        batch_size = max(1, settings.QDRANT_UPSERT_BATCH_SIZE)
        for start in range(0, len(points), batch_size):
            batch = points[start : start + batch_size]
            await self.qdrant.upsert(
                collection_name=settings.QDRANT_COLLECTION_NAME,
                points=batch,
                wait=True,
            )

    async def process_completed_job(self, doc: dict, batch_job: BatchJob) -> int:
        chunk_lookup = await self._load_chunk_lookup(doc)
        if not chunk_lookup:
            raise ValueError(
                f"No chunks were found for document {doc['_id']} while syncing embeddings."
            )

        dest = batch_job.dest
        points: list[models.PointStruct]
        file_name = getattr(dest, "file_name", None) if dest else None
        inline_responses = (
            getattr(dest, "inlined_embed_content_responses", None) if dest else None
        )

        if inline_responses:
            points = await self._points_from_inline_responses(
                doc,
                inline_responses,
                chunk_lookup,
            )
        elif file_name:
            points = await self._points_from_file_output(doc, file_name, chunk_lookup)
        else:
            raise ValueError(
                "Gemini batch job succeeded without inline embedding responses or an output file."
            )

        if not points:
            raise ValueError("No embedding vectors were produced for Qdrant sync.")

        await self._upsert_points(points)
        logger.info(
            "Synced embeddings to Qdrant",
            extra={
                "document_id": str(doc["_id"]),
                "embedding_job_id": doc.get("embedding_job_id"),
                "vector_count": len(points),
            },
        )

        if file_name:
            try:
                await self.client.files.delete(name=file_name)
            except Exception:
                logger.exception(
                    "Failed to delete Gemini output file after Qdrant sync",
                    extra={"file_name": file_name},
                )

        return len(points)

    async def poll_once(self) -> dict[str, int]:
        docs = await self.claim_documents(limit=settings.EMBEDDING_POLL_CLAIM_LIMIT)
        stats = {
            "processed": len(docs),
            "completed": 0,
            "failed": 0,
            "transient": 0,
            "timeout": 0,
        }
        if not docs:
            return stats

        semaphore = asyncio.Semaphore(settings.EMBEDDING_POLL_CONCURRENCY)

        async def handle_doc(doc: dict):
            async with semaphore:
                result = await self.poll_single_document(doc)
                if result in stats:
                    stats[result] += 1

        await asyncio.gather(*(handle_doc(doc) for doc in docs))
        return stats
