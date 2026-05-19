import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Optional
from pymongo import AsyncMongoClient
from .config import settings
from .utils.logging import get_worker_logger
from .utils.chunking import perform_text_chunking

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
        existing = await self.db.documents.find_one({
            "$or": [
                {"last_sqs_message_id": message_id},
                {"file_key": file_key, "status": "completed"}
            ]
        })
        return existing is not None

    async def run(self, bucket: str, key: str, message_id: str, content: str):
        """
        Processes document content and updates state.
        """
        log_extra = {"bucket": bucket, "key": key, "message_id": message_id}
        logger.info(f"Processing document content", extra=log_extra)
        
        doc_filter = {"file_key": key}
        
        # Initial update
        await self.db.documents.update_one(
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
            # CPU intensive chunking (could be offloaded to threadpool if strictly necessary)
            chunks = perform_text_chunking(
                content, 
                chunk_size=settings.CHUNK_SIZE, 
                chunk_overlap=settings.CHUNK_OVERLAP
            )
            
            # Atomic finalization
            await self.db.documents.update_one(
                doc_filter,
                {
                    "$set": {
                        "status": "completed", 
                        "chunk_count": len(chunks),
                        "updated_at": datetime.now(timezone.utc)
                    }
                }
            )
            logger.info("Ingestion completed successfully", extra=log_extra)

        except Exception as e:
            logger.exception("Ingestion failed", extra=log_extra)
            await self.db.documents.update_one(
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
