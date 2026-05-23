import asyncio
import signal

from pymongo import AsyncMongoClient

from app.config import settings
from app.poller import RagasEvaluationPoller
from app.utils.logging import get_worker_logger, setup_worker_logging

setup_worker_logging()
logger = get_worker_logger("ragas_eval_worker.main")


class RagasEvaluationWorker:
    def __init__(self):
        self.db_client = AsyncMongoClient(
            settings.MONGODB_URI,
            maxPoolSize=50,
            minPoolSize=5,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=10000,
            socketTimeoutMS=30000,
        )
        self.poller = RagasEvaluationPoller(self.db_client)
        self.should_exit = False

    async def _health_check(self):
        await self.db_client.admin.command("ping")
        await self.poller.ensure_indexes()

    async def run_once(self):
        await self._health_check()
        stats = await asyncio.wait_for(self.poller.poll_once(), timeout=300.0)
        logger.info("RAGAS evaluation cycle complete", extra=stats)

    async def run(self):
        await self._health_check()
        logger.info(
            "RAGAS evaluation worker started",
            extra={
                "interval_seconds": settings.RAGAS_EVAL_POLL_INTERVAL_SECONDS,
                "collection": settings.RAG_EVAL_TRACE_COLLECTION,
                "model": settings.RAGAS_EVAL_MODEL,
            },
        )

        while not self.should_exit:
            started = asyncio.get_running_loop().time()
            try:
                stats = await asyncio.wait_for(self.poller.poll_once(), timeout=300.0)
                logger.info("RAGAS evaluation cycle complete", extra=stats)
            except asyncio.TimeoutError:
                logger.error("RAGAS evaluation cycle timed out after 300 seconds")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("RAGAS evaluation cycle failed unexpectedly")

            elapsed = asyncio.get_running_loop().time() - started
            sleep_time = max(0.1, settings.RAGAS_EVAL_POLL_INTERVAL_SECONDS - elapsed)
            await self._sleep_non_blocking(sleep_time)

        logger.info("RAGAS evaluation worker stopping")

    async def shutdown(self):
        await self.poller.close()
        await self.db_client.close()
        logger.info("RAGAS evaluation worker shutdown complete")

    async def _sleep_non_blocking(self, sleep_time: float):
        for _ in range(int(sleep_time * 10)):
            if self.should_exit:
                break
            await asyncio.sleep(0.1)

    def stop(self, *_args):
        self.should_exit = True


async def main():
    worker = RagasEvaluationWorker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:
            logger.warning("Signal handlers are not available in this runtime")
    try:
        if settings.RAGAS_EVAL_RUN_ONCE:
            await worker.run_once()
        else:
            await worker.run()
    finally:
        await worker.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
