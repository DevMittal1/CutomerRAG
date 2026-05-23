import asyncio
import signal
from pymongo import AsyncMongoClient
from redis.asyncio import Redis

from app.config import settings
from app.providers.local import LocalChunkProvider
from app.providers.external import LandingAIPoller
from observability.logging import setup_worker_logging, get_worker_logger

setup_worker_logging()
logger = get_worker_logger("chunk_worker.main")

class UnifiedChunkWorker:
    """
    Unified entry point for local and external chunk workers.
    Can run in local consumer mode, external poller mode, or both.
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
        self.redis_client = Redis.from_url(
            settings.REDIS_URI,
            decode_responses=True,
        )
        self.local_provider = LocalChunkProvider(self.db_client, self.redis_client)
        self.external_poller = LandingAIPoller(self.db_client)
        self.should_exit = False
        self.active_tasks = set()

    async def _health_check(self):
        logger.info("Performing startup health check...")
        await self.db_client.admin.command("ping")
        await self.redis_client.ping()
        logger.info("Health check passed.")

    async def run_local(self):
        logger.info("Starting local chunk stream consumer...")
        await self.local_provider.run()

    async def run_external(self, interval_seconds: float = 5.0):
        logger.info("Starting external Landing AI poller...")
        await self.external_poller.ensure_indexes()
        failure_count = 0

        while not self.should_exit:
            started = asyncio.get_running_loop().time()
            try:
                stats = await asyncio.wait_for(
                    self.external_poller.poll_once(),
                    timeout=300.0
                )
                failure_count = 0
                if stats and stats.get("processed", 0) > 0:
                    logger.info(
                        "External poll cycle complete",
                        extra={
                            "processed": stats.get("processed", 0),
                            "completed": stats.get("completed", 0),
                            "failed": stats.get("failed", 0),
                            "transient": stats.get("transient", 0),
                            "timeout": stats.get("timeout", 0),
                        }
                    )
            except asyncio.TimeoutError:
                logger.error("External poll cycle timed out (exceeded 300 seconds limit)")
                failure_count += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error during external poll cycle: {e}")
                failure_count += 1

            elapsed = asyncio.get_running_loop().time() - started
            
            # Non-blocking sleep with backoff
            base_sleep = max(0.1, interval_seconds - elapsed)
            if failure_count > 0:
                backoff_sleep = min(60.0, 2.0 ** failure_count)
                sleep_time = max(base_sleep, backoff_sleep)
            else:
                sleep_time = base_sleep

            for _ in range(int(sleep_time * 10)):
                if self.should_exit:
                    break
                await asyncio.sleep(0.1)

    async def run(self):
        await self._health_check()
        mode = settings.CHUNK_WORKER_MODE.lower()
        logger.info(f"Running Chunk Worker in '{mode}' mode")

        tasks = []
        if mode in ("local", "both"):
            tasks.append(asyncio.create_task(self.run_local()))
        if mode in ("external", "poller", "both"):
            tasks.append(asyncio.create_task(self.run_external()))

        if not tasks:
            logger.error(f"Invalid CHUNK_WORKER_MODE: '{settings.CHUNK_WORKER_MODE}'")
            return

        self.active_tasks.update(tasks)
        await asyncio.gather(*tasks, return_exceptions=True)

        logger.info("Cleaning up connections and shutting down...")
        try:
            await self.redis_client.aclose()
        except Exception as e:
            logger.warning(f"Failed to close Redis client connection: {e}")
        try:
            await self.external_poller.close()
        except Exception as e:
            logger.warning(f"Failed to close HTTP client: {e}")
        try:
            await self.db_client.close()
        except Exception as e:
            logger.warning(f"Failed to close MongoDB client: {e}")
        logger.info("Chunk Worker shutdown complete.")

    def stop(self, *args):
        logger.info("Stop signal received. Initiating graceful shutdown...")
        self.should_exit = True
        self.local_provider.stop()

async def main():
    worker = UnifiedChunkWorker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:
            logger.warning(f"add_signal_handler not implemented for signal {sig}, fallback used.")
    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())
