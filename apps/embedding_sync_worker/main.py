import asyncio
import signal

from pymongo import AsyncMongoClient

from .app.config import settings
from .app.poller import GeminiEmbeddingPoller
from .app.utils.logging import get_worker_logger, setup_worker_logging

setup_worker_logging()
logger = get_worker_logger("rag_embedding_sync.main")


class EmbeddingSyncWorker:
    """
    Periodically polls Gemini embedding batch jobs and syncs results into Qdrant.
    """

    def __init__(self):
        self.db_client = AsyncMongoClient(
            settings.MONGODB_URI,
            maxPoolSize=50,
            minPoolSize=5,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=10000,
            socketTimeoutMS=30000,
        )
        self.poller = GeminiEmbeddingPoller(self.db_client)
        self.should_exit = False

    async def _health_check(self):
        await self.db_client.admin.command("ping")
        await self.poller.ensure_indexes()
        await self.poller.ensure_qdrant_collection()

    async def run_once(self):
        await self._health_check()
        stats = await asyncio.wait_for(self.poller.poll_once(), timeout=300.0)
        logger.info("Embedding poll cycle complete", extra=stats)

    async def run(self):
        await self._health_check()
        logger.info(
            "Embedding sync worker started",
            extra={
                "interval_seconds": settings.EMBEDDING_POLL_INTERVAL_SECONDS,
                "qdrant_collection": settings.QDRANT_COLLECTION_NAME,
            },
        )

        while not self.should_exit:
            started = asyncio.get_running_loop().time()
            try:
                stats = await asyncio.wait_for(self.poller.poll_once(), timeout=300.0)
                logger.info("Embedding poll cycle complete", extra=stats)
            except asyncio.TimeoutError:
                logger.error("Embedding poll cycle timed out after 300 seconds")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Embedding poll cycle failed unexpectedly")

            elapsed = asyncio.get_running_loop().time() - started
            sleep_time = max(0.1, settings.EMBEDDING_POLL_INTERVAL_SECONDS - elapsed)
            await self._sleep_non_blocking(sleep_time)

        logger.info("Embedding sync worker stopping")

    async def shutdown(self):
        await self.poller.close()
        await self.db_client.close()
        logger.info("Embedding sync worker shutdown complete")

    async def _sleep_non_blocking(self, sleep_time: float):
        for _ in range(int(sleep_time * 10)):
            if self.should_exit:
                break
            await asyncio.sleep(0.1)

    def stop(self, *_args):
        self.should_exit = True


async def main():
    worker = EmbeddingSyncWorker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:
            logger.warning("Signal handlers are not available in this runtime")
    try:
        if settings.EMBEDDING_POLL_RUN_ONCE:
            await worker.run_once()
        else:
            await worker.run()
    finally:
        await worker.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
