import asyncio
from datetime import datetime, timezone
from typing import Literal

import httpx
from pymongo import AsyncMongoClient, ReturnDocument
from redis.asyncio import Redis

from .config import settings
from rag_queue.retry import should_retry_http, get_exponential_backoff_delay
from observability.logging import get_worker_logger

logger = get_worker_logger("worker.processor")

IngestionRoute = Literal["internal", "external", "skip"]


class IngestionProcessor:
    """
    Production-hardened ingestion processor.
    - MongoDB owns document state and idempotency.
    - Redis Streams fan out internal chunk work.
    - External parsing stays explicit and retry-aware.
    """

    def __init__(self, db_client: AsyncMongoClient, redis_client: Redis):
        self.db_client = db_client
        self.db = self.db_client[settings.MONGODB_DB_NAME]
        self.redis_client = redis_client

    async def is_already_processed(self, file_key: str) -> bool:
        """
        The document record is the source of truth for ingestion ownership.
        """
        existing = await self.db[settings.COLL_DOCUMENTS].find_one(
            {
                "file_key": file_key,
                "status": {
                    "$in": [
                        "queued_for_chunking",
                        "chunking",
                        "landing_ai_pending",
                        "completed",
                    ]
                },
            }
        )
        return existing is not None

    def _should_retry(
        self,
        exception: Exception,
        attempt: int,
        max_retries: int,
    ) -> bool:
        return should_retry_http(exception, attempt, max_retries)

    def _get_retry_delay(
        self,
        attempt: int,
        base_delay: float,
        max_delay: float,
    ) -> float:
        return get_exponential_backoff_delay(attempt, base_delay, max_delay)

    async def _attempt_submission(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        files: dict,
        data: dict,
    ) -> str:
        response = await client.post(
            f"{settings.LANDING_AI_BASE_URL}/parse/jobs",
            headers=headers,
            files=files,
            data=data,
        )
        response.raise_for_status()
        return response.json()["job_id"]

    async def submit_to_landing_ai(self, file_content: bytes, filename: str) -> str:
        if not settings.LANDING_AI_API_KEY:
            raise ValueError("LANDING_AI_API_KEY is not set")

        headers = {"Authorization": f"Bearer {settings.LANDING_AI_API_KEY}"}
        files = {"document": (filename, file_content)}
        data = {"model": "dpt-2-latest"}

        max_retries = 5
        async with httpx.AsyncClient(timeout=60.0) as client:
            for attempt in range(1, max_retries + 1):
                try:
                    logger.info(
                        f"Submitting to Landing AI (attempt {attempt}/{max_retries})"
                    )
                    return await self._attempt_submission(client, headers, files, data)
                except Exception as exc:
                    if not self._should_retry(exc, attempt, max_retries):
                        raise

                    delay = self._get_retry_delay(attempt, 1.0, 10.0)
                    logger.warning(
                        f"Landing AI attempt {attempt} failed ({exc}). Retrying in {delay:.2f}s..."
                    )
                    await asyncio.sleep(delay)

        raise RuntimeError("Landing AI submission failed after all retries.")

    async def enqueue_internal_chunking(
        self,
        bucket: str,
        key: str,
        message_id: str,
    ) -> str:
        stream_id = await self.redis_client.xadd(
            settings.REDIS_CHUNK_STREAM,
            {
                "bucket": bucket,
                "key": key,
                "message_id": message_id,
            },
        )
        return str(stream_id)

    async def start_ingestion(
        self,
        bucket: str,
        key: str,
        message_id: str,
    ) -> IngestionRoute:
        """
        Claims the document record and decides the downstream processing route.
        """
        log_extra = {"bucket": bucket, "key": key, "message_id": message_id}

        document = await self.db[settings.COLL_DOCUMENTS].find_one_and_update(
            {
                "file_key": key,
                "status": {"$in": ["pending", "uploaded"]},
            },
            {
                "$set": {
                    "status": "processing",
                    "last_sqs_message_id": message_id,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
            return_document=ReturnDocument.AFTER,
        )

        if document is None:
            existing = await self.db[settings.COLL_DOCUMENTS].find_one(
                {"file_key": key},
                {"status": 1},
            )
            if existing is None:
                logger.warning(
                    "Skipping S3 event with no matching document record",
                    extra=log_extra,
                )
            else:
                logger.info(
                    "Skipping S3 event because document is already owned or terminal",
                    extra={**log_extra, "status": existing.get("status")},
                )
            return "skip"

        if not document.get("use_external_parser", False):
            stream_id = await self.enqueue_internal_chunking(
                bucket=bucket,
                key=key,
                message_id=message_id,
            )
            await self.db[settings.COLL_DOCUMENTS].update_one(
                {"_id": document["_id"]},
                {
                    "$set": {
                        "status": "queued_for_chunking",
                        "chunk_stream_id": stream_id,
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
            logger.info(
                "Document queued for internal chunking",
                extra={**log_extra, "stream_id": stream_id},
            )
            return "internal"

        logger.info("Document claimed for external parsing", extra=log_extra)
        return "external"

    async def submit_external_parser(self, key: str, content_bytes: bytes) -> None:
        """
        Submits a claimed document to Landing AI and records the job metadata.
        """
        log_extra = {"key": key}
        doc_filter = {"file_key": key}

        try:
            job_id = await self.submit_to_landing_ai(content_bytes, key)
            await self.db[settings.COLL_DOCUMENTS].update_one(
                doc_filter,
                {
                    "$set": {
                        "status": "landing_ai_pending",
                        "landing_ai_job_id": job_id,
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
            logger.info(
                f"Document submitted to Landing AI. JobID: {job_id}",
                extra=log_extra,
            )
        except Exception as exc:
            logger.exception("Landing AI submission failed", extra=log_extra)
            await self.db[settings.COLL_DOCUMENTS].update_one(
                doc_filter,
                {
                    "$set": {
                        "status": "failed",
                        "error": str(exc),
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
            raise


async def heartbeat_extender(
    sqs_client,
    queue_url: str,
    receipt_handle: str,
    interval: int = 15,
):
    """
    Keeps the SQS message hidden while the ingestion worker is still active.
    """
    logger.info("Heartbeat started")
    try:
        while True:
            await asyncio.sleep(interval)
            await sqs_client.change_message_visibility(
                QueueUrl=queue_url,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=30,
            )
            logger.info("Heartbeat: extended visibility timeout")
    except asyncio.CancelledError:
        logger.info("Heartbeat stopped")
        raise
    except Exception as exc:
        logger.error(f"Heartbeat failed: {exc}")
