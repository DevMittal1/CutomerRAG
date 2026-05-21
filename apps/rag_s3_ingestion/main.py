import asyncio
import json
import signal

import aioboto3
from pymongo import AsyncMongoClient
from redis.asyncio import Redis

from .app.config import settings
from .app.processor import IngestionProcessor, heartbeat_extender
from .app.utils.logging import get_worker_logger, setup_worker_logging

setup_worker_logging()
logger = get_worker_logger("rag_worker.main")


class ProductionRAGWorker:
    """
    S3 ingestion worker that stays I/O-bound and only routes work downstream.
    """

    def __init__(self):
        self.session = aioboto3.Session()
        self.db_client = AsyncMongoClient(settings.MONGODB_URI)
        self.redis_client: Redis = Redis.from_url(
            settings.REDIS_URI,
            decode_responses=True,
        )
        self.processor = IngestionProcessor(self.db_client, self.redis_client)
        self.semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_TASKS)
        self.should_exit = False
        self.active_tasks: set[asyncio.Task] = set()

    async def _health_check(self, sqs):
        logger.info("Performing startup health check...")
        await sqs.get_queue_attributes(
            QueueUrl=settings.AWS_SQS_QUEUE_URL,
            AttributeNames=["QueueArn"],
        )
        await self.db_client.admin.command("ping")
        await self.redis_client.ping()  # pyright: ignore[reportGeneralTypeIssues]
        logger.info("Health check passed.")

    async def run(self):
        async with self.session.client(
            "sqs",
            region_name=settings.AWS_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        ) as sqs:
            await self._health_check(sqs)
            logger.info(
                f"Worker polling started (Concurrency={settings.MAX_CONCURRENT_TASKS})"
            )

            while not self.should_exit:
                try:
                    response = await sqs.receive_message(
                        QueueUrl=settings.AWS_SQS_QUEUE_URL,
                        MaxNumberOfMessages=min(10, settings.MAX_CONCURRENT_TASKS),
                        WaitTimeSeconds=settings.AWS_SQS_WAIT_TIME_SECONDS,
                        AttributeNames=["All"],
                    )
                    for message in response.get("Messages", []):
                        task = asyncio.create_task(
                            self._process_message_wrapper(sqs, message)
                        )
                        self.active_tasks.add(task)
                        task.add_done_callback(self.active_tasks.discard)
                except Exception as exc:
                    logger.error(f"Loop error: {exc}")
                    await asyncio.sleep(5)

        if self.active_tasks:
            logger.info(
                f"Waiting for {len(self.active_tasks)} active tasks to finish..."
            )
            await asyncio.gather(*self.active_tasks, return_exceptions=True)

        await self.db_client.close()
        await self.redis_client.aclose()
        logger.info("Worker shutdown complete.")

    async def _process_message_wrapper(self, sqs, message):
        async with self.semaphore:
            receipt_handle = message["ReceiptHandle"]
            message_id = message["MessageId"]
            heartbeat = asyncio.create_task(
                heartbeat_extender(sqs, settings.AWS_SQS_QUEUE_URL, receipt_handle)
            )

            try:
                await self.redis_client.set(  # pyright: ignore[reportGeneralTypeIssues]
                    f"active_task:{message_id}",
                    "processing",
                    ex=600,
                )
                body = json.loads(message["Body"])
                records = body.get("Records", [])

                for record in records:
                    bucket = record["s3"]["bucket"]["name"]
                    key = record["s3"]["object"]["key"]

                    if await self.processor.is_already_processed(key):
                        logger.info(
                            "Skipping already routed or completed file",
                            extra={"key": key, "msg_id": message_id},
                        )
                        continue

                    route = await self.processor.start_ingestion(
                        bucket=bucket,
                        key=key,
                        message_id=message_id,
                    )
                    if route in {"internal", "skip"}:
                        continue

                    async with self.session.client(
                        "s3",
                        region_name=settings.AWS_REGION,
                        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                    ) as s3:
                        obj = await s3.get_object(Bucket=bucket, Key=key)
                        data = await obj["Body"].read()

                    await self.processor.submit_external_parser(
                        key=key,
                        content_bytes=data,
                    )

                await sqs.delete_message(
                    QueueUrl=settings.AWS_SQS_QUEUE_URL,
                    ReceiptHandle=receipt_handle,
                )
            except Exception as exc:
                logger.error(
                    f"Message processing failed: {exc}",
                    extra={"msg_id": message_id},
                )
            finally:
                try:
                    await self.redis_client.delete(  # pyright: ignore[reportGeneralTypeIssues]
                        f"active_task:{message_id}"
                    )
                except Exception as exc:
                    logger.error(f"Failed to remove task from Redis active_tasks: {exc}")
                heartbeat.cancel()

    def stop(self, *args):
        self.should_exit = True


async def main():
    worker = ProductionRAGWorker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:
            logger.warning(f"Signal handler not available for {sig}")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
