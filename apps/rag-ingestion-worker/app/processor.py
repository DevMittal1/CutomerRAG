import asyncio
import httpx
import hashlib
from datetime import datetime, timezone
from typing import Optional
from pymongo import AsyncMongoClient
from .config import settings
from .utils.logging import get_worker_logger

logger = get_worker_logger("worker.processor")

class IngestionProcessor:
    """
    Production-hardened ingestion processor.
    - Idempotency via MongoDB unique tracking.
    - S3 fetching via aioboto3 (handled in main).
    - Status transitions and error classification.
    """
    
    def __init__(self, db_client: AsyncMongoClient):
        self.db_client = db_client
        self.db = self.db_client[settings.MONGODB_DB_NAME]

    async def is_already_processed(self, message_id: str, file_key: str) -> bool:
        """
        Idempotency check:
        Ensures we don't process the same SQS message or file key if already completed.
        """
        # Check by SQS Message ID or a successfully ingested file_key
        existing = await self.db[settings.COLL_DOCUMENTS].find_one({
            "$or": [
                {"last_sqs_message_id": message_id},
                {"file_key": file_key, "status": "completed"}
            ]
        })
        return existing is not None

    async def submit_to_landing_ai(self, file_content: bytes, filename: str) -> str:
        """
        Uploads file to Landing AI and returns job_id.
        """
        if not settings.LANDING_AI_API_KEY:
            raise ValueError("LANDING_AI_API_KEY is not set")

        headers = {
            "Authorization": f"Bearer {settings.LANDING_AI_API_KEY}"
        }
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            files = {
                "document": (filename, file_content)
            }
            data = {
                "model": "dpt-2-latest"
            }
            
            response = await client.post(
                f"{settings.LANDING_AI_BASE_URL}/parse/jobs",
                headers=headers,
                files=files,
                data=data
            )
            response.raise_for_status()
            result = response.json()
            return result["job_id"]

    async def run(self, bucket: str, key: str, message_id: str, content_bytes: bytes):
        """
        Submits document to Landing AI and updates state to pending.
        """
        log_extra = {"bucket": bucket, "key": key, "message_id": message_id}
        logger.info(f"Submitting document to Landing AI", extra=log_extra)
        
        doc_filter = {"file_key": key}
        
        # Initial update
        await self.db[settings.COLL_DOCUMENTS].update_one(
            doc_filter,
            {
                "$set": {
                    "status": "processing",
                    "last_sqs_message_id": message_id,
                    "updated_at": datetime.now(timezone.utc)
                }
            },
            upsert=True
        )
        try:
            # 1. Submit to Landing AI
            job_id = await self.submit_to_landing_ai(content_bytes, key)
            
            # 2. Update state to landing_ai_pending
            await self.db[settings.COLL_DOCUMENTS].update_one(
                doc_filter,
                {
                    "$set": {
                        "status": "landing_ai_pending", 
                        "landing_ai_job_id": job_id,
                        "updated_at": datetime.now(timezone.utc)
                    }
                }
            )
            logger.info(f"Document submitted to Landing AI. JobID: {job_id}", extra=log_extra)

        except Exception as e:
            logger.exception("Landing AI submission failed", extra=log_extra)
            await self.db[settings.COLL_DOCUMENTS].update_one(
                doc_filter,
                {
                    "$set": {
                        "status": "failed",
                        "error": str(e),
                        "updated_at": datetime.now(timezone.utc)
                    }
                }
            )
            raise

async def heartbeat_extender(sqs_client, queue_url: str, receipt_handle: str, interval: int = 15):
    """
    Background loop to keep SQS message hidden while processing is active.
    Prevents other workers from picking up the same message.
    """
    logger.info("Heartbeat started")
    try:
        while True:
            await asyncio.sleep(interval)
            await sqs_client.change_message_visibility(
                QueueUrl=queue_url,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=30 # Extend for another 30s
            )
            logger.info("Heartbeat: extended visibility timeout")
    except asyncio.CancelledError:
        logger.info("Heartbeat stopped")
    except Exception as e:
        logger.error(f"Heartbeat failed: {e}")
