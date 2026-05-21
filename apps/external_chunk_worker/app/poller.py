import asyncio
import uuid
import httpx
from datetime import datetime, timezone, timedelta
from bson import ObjectId
from pymongo import AsyncMongoClient
from .config import settings
from .embeddings import trigger_inline_batch_embeddings_async
from .utils.logging import get_worker_logger
from .utils.constants import RELEASE_LOCK_UPDATE

logger = get_worker_logger("worker.poller")

class LandingAIPoller:
    """
    Production-grade Landing AI status poller.
    - Reusable connection pool client.
    - Bounded concurrent polling.
    - Total job timeout validation.
    - Adaptive polling backoff with rate-limit and transient error handling.
    - Idempotent chunk inserts preventing duplicate data.
    - Explicit exception sorting and signal cancellation awareness.
    - Distributed job claiming & atomic worker locks to support horizontal scaling.
    """
    def __init__(self, db_client: AsyncMongoClient):
        self.db_client = db_client
        self.db = self.db_client[settings.MONGODB_DB_NAME]
        self.worker_id = str(uuid.uuid4())
        self.headers = {
            "Authorization": f"Bearer {settings.LANDING_AI_API_KEY}"
        }
        # Reusable connection pool client with hardened parameters
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=30.0,
                write=30.0,
                pool=10.0
            ),
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20
            )
        )

    async def close(self):
        """Closes the reusable HTTP client connection pool."""
        await self.client.aclose()

    async def ensure_indexes(self):
        """Ensures that the unique chunk constraints exist in MongoDB for horizontal scalability safety."""
        try:
            await self.db[settings.COLL_CHUNKS].create_index(
                [("job_id", 1), ("chunk_index", 1)],
                unique=True
            )
            logger.info("MongoDB unique chunks indexes verified/created successfully.")
        except Exception as e:
            logger.warning(f"Could not verify/create unique indexes: {e}")

    async def check_job_status(self, job_id: str) -> dict:
        """
        Polls Landing AI for a single job status using a shared reusable client.
        """
        url = f"{settings.LANDING_AI_BASE_URL}/parse/jobs/{job_id}"
        response = await self.client.get(url, headers=self.headers)
        response.raise_for_status()
        return response.json()

    def _calculate_next_poll_delay(self, attempts: int) -> float:
        """Adaptive backoff polling intervals to prevent API gateway spam."""
        if attempts <= 5:
            return 5.0
        elif attempts <= 20:
            return 30.0
        return 120.0

    async def _handle_transient_failure(self, doc: dict, _error_msg: str):
        """Increments attempts, schedules next poll, and unsets worker locks."""
        attempts = doc.get("poll_attempts", 0) + 1
        delay = self._calculate_next_poll_delay(attempts)
        next_poll_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        
        await self.db[settings.COLL_DOCUMENTS].update_one(
            {"_id": doc["_id"]},
            {
                "$set": {
                    "poll_attempts": attempts,
                    "next_poll_at": next_poll_at,
                    "updated_at": datetime.now(timezone.utc)
                },
                **RELEASE_LOCK_UPDATE
            }
        )

    async def _handle_terminal_failure(self, doc: dict, reason: str):
        """Marks the document as permanently failed and unsets worker locks."""
        job_id = doc.get("landing_ai_job_id", "Unknown")
        logger.error(f"Job {job_id} failed: {reason}")
        await self.db[settings.COLL_DOCUMENTS].update_one(
            {"_id": doc["_id"]},
            {
                "$set": {
                    "status": "failed",
                    "error": reason,
                    "updated_at": datetime.now(timezone.utc)
                },
                **RELEASE_LOCK_UPDATE
            }
        )

    async def process_completed_job(self, doc: dict, result: dict):
        """
        Handles data extraction and storage for a completed Landing AI job.
        Processes chunks in memory-efficient batches of 100 and immediately deletes them.
        """
        job_id = doc["landing_ai_job_id"]
        file_key = doc["file_key"]
        
        logger.info(f"Processing completed job: {job_id} for {file_key}")
        
        data = result.get("data", {})
        chunks = data.get("chunks", [])
        
        # 1. Clear any previous chunks for this job first to guarantee idempotency
        await self.db[settings.COLL_CHUNKS].delete_many({"job_id": job_id})
        
        batch = []
        chunk_count = 0
        
        # Parse result metadata
        version = result.get("version")
        received_at = result.get("received_at")
        
        for idx, chunk in enumerate(chunks):
            grounding = chunk.get("grounding", {})
            box = grounding.get("box", {})

            chunk_doc = {
                "_id": ObjectId(),
                "document_id": doc["_id"],
                "job_id": job_id,
                "source_url": file_key,
                "chunk_index": idx,
                "chunk_id": chunk.get("id"),
                "content": chunk.get("markdown", ""),
                "chunk_type": chunk.get("type"),
                "page": grounding.get("page"),
                "bounding_box": {
                    "left": box.get("left"),
                    "top": box.get("top"),
                    "right": box.get("right"),
                    "bottom": box.get("bottom"),
                },
                "metadata": {
                    "landingai_version": version,
                    "received_at": received_at,
                },
                "created_at": datetime.now(timezone.utc),
            }
            batch.append(chunk_doc)
            chunk_count += 1

            if len(batch) >= 100:
                try:
                    await self.db[settings.COLL_CHUNKS].insert_many(batch, ordered=False)
                except Exception as e:
                    logger.warning(f"Handled duplicate chunks during batch write for job {job_id}: {e}")
                batch.clear()

        # Insert remaining chunks in the last batch
        if batch:
            try:
                await self.db[settings.COLL_CHUNKS].insert_many(batch, ordered=False)
            except Exception as e:
                logger.warning(f"Handled duplicate chunks during final batch write for job {job_id}: {e}")
            batch.clear()

        logger.info(f"Idempotently inserted {chunk_count} chunks in memory-bounded batches for job {job_id}")
        embedding_fields = await self.submit_embeddings_for_chunks(doc, chunks)
        
        # 2. Update document status and release locks
        await self.db[settings.COLL_DOCUMENTS].update_one(
            {"_id": doc["_id"]},
            {
                "$set": {
                    "status": "completed",
                    "chunk_count": chunk_count,
                    "updated_at": datetime.now(timezone.utc),
                    **embedding_fields,
                },
                **RELEASE_LOCK_UPDATE
            }
        )
        
        del result
        del chunks

    async def submit_embeddings_for_chunks(self, doc: dict, chunks: list[dict]) -> dict:
        now = datetime.now(timezone.utc)
        requests = []
        for idx, chunk in enumerate(chunks):
            chunk_text = chunk.get("markdown")
            if not chunk_text:
                continue
            requests.append(
                {
                    "output_dimensionality": settings.GEMINI_EMBEDDING_OUTPUT_DIMENSIONALITY,
                    "content": {
                        "parts": [
                            {"text": f"task: search_document | text: {chunk_text}"}
                        ]
                    },
                    "metadata": {
                        "document_id": str(doc["_id"]),
                        "file_key": doc["file_key"],
                        "chunk_index": str(idx),
                        "chunk_id": chunk.get("id", "") or "",
                        "landing_ai_job_id": doc.get("landing_ai_job_id", ""),
                    },
                }
            )

        if not requests:
            return {
                "embedding_status": "skipped",
                "embedding_error": "No chunk content available for embedding submission.",
                "embedding_updated_at": now,
            }

        if not settings.GOOGLE_API_KEY:
            return {
                "embedding_status": "skipped",
                "embedding_error": "GOOGLE_API_KEY is not configured.",
                "embedding_updated_at": now,
            }

        try:
            embedding_job_id = await trigger_inline_batch_embeddings_async(
                requests=requests,
                api_key=settings.GOOGLE_API_KEY,
                model=settings.GEMINI_EMBEDDING_MODEL,
                display_name=settings.GEMINI_EMBEDDING_DISPLAY_NAME,
                max_inline_bytes=settings.GEMINI_EMBEDDING_MAX_INLINE_BYTES,
            )
            return {
                "embedding_job_id": embedding_job_id,
                "embedding_status": "submitted",
                "embedding_model": settings.GEMINI_EMBEDDING_MODEL,
                "embedding_poll_attempts": 0,
                "embedding_submitted_at": now,
                "embedding_next_poll_at": now,
                "embedding_updated_at": now,
                "embedding_error": None,
            }
        except Exception as exc:
            logger.exception(
                "Embedding submission failed",
                extra={"file_key": doc["file_key"]},
            )
            return {
                "embedding_status": "failed",
                "embedding_model": settings.GEMINI_EMBEDDING_MODEL,
                "embedding_poll_attempts": 0,
                "embedding_updated_at": now,
                "embedding_error": str(exc),
            }

    def _check_job_timeout(self, doc: dict, job_id: str, now: datetime) -> bool:
        """
        Validates total job age. If it exceeds 1 hour (3600s), returns True.
        """
        created_at = doc.get("created_at")
        if not created_at:
            return False
        if created_at.tzinfo is None:
            created_at = created_at.replace(timezone.utc)
        
        age = (now - created_at).total_seconds()
        if age > 3600:
            logger.error(f"Job {job_id} exceeded maximum timeout of 3600 seconds. Failing.")
            return True
        return False

    async def _handle_api_response(self, doc: dict, result: dict) -> str:
        """
        Evaluates Landing AI API job response status.
        """
        job_id = doc["landing_ai_job_id"]
        status = result.get("status")

        if status == "completed":
            await self.process_completed_job(doc, result)
            return "completed"
        
        if status in ["failed", "error"]:
            reason = result.get("failure_reason", "Unknown Landing AI error")
            await self._handle_terminal_failure(doc, reason)
            return "failed"
        
        progress = result.get("progress", 0)
        logger.info(f"Job {job_id} is still processing ({progress * 100:.1f}%). Incrementing poll schedule.")
        await self._handle_transient_failure(doc, f"Still processing ({progress * 100:.1f}%)")
        return "transient"

    async def _handle_poll_exception(self, doc: dict, job_id: str, exc: Exception) -> str:
        """
        Sorts, logs, and acts upon exceptions during status polling.
        """
        if isinstance(exc, asyncio.CancelledError):
            logger.info(f"Polling cancelled for job {job_id}")
            raise exc

        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            if status_code == 429 or status_code >= 500:
                logger.warning(f"Transient HTTP {status_code} checking status for job {job_id}: {exc}. Retrying.")
                await self._handle_transient_failure(doc, f"HTTP {status_code} transient error")
                return "transient"
            
            logger.error(f"Terminal HTTP {status_code} checking status for job {job_id}: {exc}")
            await self._handle_terminal_failure(doc, f"Terminal HTTP {status_code} client error")
            return "failed"

        if isinstance(exc, (httpx.TimeoutException, httpx.RequestError)):
            logger.warning(f"Transient network/timeout checking status for job {job_id}: {exc}. Retrying.")
            await self._handle_transient_failure(doc, "Transient network/timeout error")
            return "transient"

        logger.exception(f"Unexpected error checking status for job {job_id}: {exc}")
        await self._handle_transient_failure(doc, f"Unexpected error: {str(exc)}")
        return "transient"

    async def poll_single_document(self, doc: dict) -> str:
        """
        Polls and processes a single document tracking record.
        Returns one of: "completed", "failed", "transient", "timeout".
        """
        job_id = doc.get("landing_ai_job_id")
        if not job_id:
            logger.warning(f"No job_id found in document {doc.get('file_key')}, marking failed.")
            await self._handle_terminal_failure(doc, "Missing landing_ai_job_id in DB record.")
            return "failed"

        # 1. Timeout Check: Check total job age (Timeout if > 1 hour)
        if self._check_job_timeout(doc, job_id, datetime.now(timezone.utc)):
            await self._handle_terminal_failure(doc, "Landing AI parsing job timeout (exceeded 1 hour)")
            return "timeout"

        # 2. Query Landing AI
        try:
            result = await self.check_job_status(job_id)
            return await self._handle_api_response(doc, result)
        except Exception as e:
            return await self._handle_poll_exception(doc, job_id, e)

    async def claim_documents(self, limit: int = 20) -> list:
        """
        Atomically queries and locks pending documents for this specific worker instance.
        Uses a lock expiration duration to allow other workers to reclaim if this node crashes.
        """
        now = datetime.now(timezone.utc)
        claimed_docs = []

        for _ in range(limit):
            doc = await self.db[settings.COLL_DOCUMENTS].find_one_and_update(
                {
                    "status": "landing_ai_pending",
                    "$and": [
                        {
                            "$or": [
                                {"next_poll_at": {"$exists": False}},
                                {"next_poll_at": {"$lte": now}}
                            ]
                        },
                        {
                            "$or": [
                                {"lock_expires_at": {"$exists": False}},
                                {"lock_expires_at": {"$lte": now}}
                            ]
                        }
                    ]
                },
                {
                    "$set": {
                        "locked_by": self.worker_id,
                        "lock_expires_at": now + timedelta(seconds=120),
                        "updated_at": now
                    }
                },
                return_document=True
            )

            if not doc:
                break

            claimed_docs.append(doc)

        return claimed_docs

    async def poll_once(self) -> dict:
        """
        Performs one cycle of polling for all pending jobs concurrently with bounded concurrency.
        Returns a dictionary of execution metrics for observability.
        """
        # Atomically claim jobs to prevent multiple workers from polling the same items
        pending_docs = await self.claim_documents(limit=100)
        
        stats = {
            "processed": len(pending_docs),
            "completed": 0,
            "failed": 0,
            "transient": 0,
            "timeout": 0,
        }
        
        if not pending_docs:
            return stats

        semaphore = asyncio.Semaphore(20)

        async def poll_single_doc_wrapper(doc):
            async with semaphore:
                try:
                    res = await self.poll_single_document(doc)
                    if res in stats:
                        stats[res] += 1
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Failed to poll doc {doc.get('file_key')}: {e}")
                    stats["transient"] += 1

        tasks = [poll_single_doc_wrapper(doc) for doc in pending_docs]
        await asyncio.gather(*tasks)
        return stats
