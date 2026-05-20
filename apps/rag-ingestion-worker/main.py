import asyncio
import json
import signal
import aioboto3
from pymongo import AsyncMongoClient
from .app.config import settings
from .app.utils.logging import setup_worker_logging, get_worker_logger
from .app.processor import IngestionProcessor, heartbeat_extender

setup_worker_logging()
logger = get_worker_logger("rag_worker.main")

class ProductionRAGWorker:
    """
    Hardened Production Worker.
    - aioboto3 for non-blocking IO.
    - Concurrency pool via Semaphore.
    - Graceful shutdown.
    - Batch polling (up to 10).
    """
    
    def __init__(self):
        self.session = aioboto3.Session()
        self.db_client = AsyncMongoClient(settings.MONGODB_URI)
        self.processor = IngestionProcessor(self.db_client)
        self.semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_TASKS)
        self.should_exit = False
        self.active_tasks = set()

    async def _health_check(self, sqs):
        """Startup validation."""
        logger.info("Performing startup health check...")
        try:
            # SQS check
            await sqs.get_queue_attributes(
                QueueUrl=settings.AWS_SQS_QUEUE_URL, 
                AttributeNames=['QueueArn']
            )
            # Mongo check
            await self.db_client.admin.command('ping')
            logger.info("Health check passed.")
        except Exception as e:
            logger.error(f"Startup health check FAILED: {e}")
            raise

    async def run(self):
        """Main Loop."""
        async with self.session.client("sqs", 
            region_name=settings.AWS_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY
        ) as sqs:
            
            await self._health_check(sqs)
            
            logger.info(f"Worker polling started (Concurrency={settings.MAX_CONCURRENT_TASKS})")

            while not self.should_exit:
                try:
                    response = await sqs.receive_message(
                        QueueUrl=settings.AWS_SQS_QUEUE_URL,
                        MaxNumberOfMessages=min(10, settings.MAX_CONCURRENT_TASKS),
                        WaitTimeSeconds=settings.AWS_SQS_WAIT_TIME_SECONDS,
                        AttributeNames=['All']
                    )
                    
                    messages = response.get("Messages", [])
                    for msg in messages:
                        # Schedule concurrent task
                        task = asyncio.create_task(self._process_message_wrapper(sqs, msg))
                        self.active_tasks.add(task)
                        task.add_done_callback(self.active_tasks.discard)

                except Exception as e:
                    logger.error(f"Loop error: {e}")
                    await asyncio.sleep(5)

        # Shutdown cleanup
        if self.active_tasks:
            logger.info(f"Waiting for {len(self.active_tasks)} active tasks to finish...")
            await asyncio.gather(*self.active_tasks, return_exceptions=True)
        
        await self.db_client.close()
        logger.info("Worker shutdown complete.")

    async def _process_message_wrapper(self, sqs, message):
        """Concurrency-aware wrapper."""
        async with self.semaphore:
            receipt_handle = message["ReceiptHandle"]
            message_id = message["MessageId"]
            
            # 1. Heartbeat Task (Background)
            heartbeat = asyncio.create_task(heartbeat_extender(sqs, settings.AWS_SQS_QUEUE_URL, receipt_handle))
            
            try:
                body = json.loads(message["Body"])
                # Handle potential S3 event records
                records = body.get("Records", [])
                for record in records:
                    bucket = record["s3"]["bucket"]["name"]
                    key = record["s3"]["object"]["key"]
                    
                    # Idempotency Check
                    if await self.processor.is_already_processed(message_id, key):
                        logger.warning(f"Skipping already processed message/file", extra={"key": key, "msg_id": message_id})
                        continue

                    async with self.session.client("s3",
                        region_name=settings.AWS_REGION,
                        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY
                    ) as s3:
                        obj = await s3.get_object(Bucket=bucket, Key=key)
                        content_bytes = await obj['Body'].read()

                    # 3. Process (Submission to Landing AI)
                    await self.processor.run(bucket, key, message_id, content_bytes)

                # 4. Successful processing: Delete
                await sqs.delete_message(
                    QueueUrl=settings.AWS_SQS_QUEUE_URL,
                    ReceiptHandle=receipt_handle
                )
            
            except Exception as e:
                logger.error(f"Message processing failed: {e}", extra={"msg_id": message_id})
            finally:
                heartbeat.cancel()

    def stop(self, *args):
        self.should_exit = True

async def main():
    worker = ProductionRAGWorker()
    
    # OS Signal Handling
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_event_loop().add_signal_handler(sig, worker.stop)
        
    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())
