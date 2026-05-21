import asyncio
import signal
from pymongo import AsyncMongoClient
from .app.config import settings
from .app.utils.logging import setup_worker_logging, get_worker_logger
from .app.poller import LandingAIPoller

setup_worker_logging()
logger = get_worker_logger("rag_poller.main")

class ProductionLandingAIPollerWorker:
    """
    Hardened Production Landing AI Poller Worker.
    - Decoupled from SQS ingestion queue.
    - Non-blocking S3 pre-signed URI alignment.
    - Custom PyMongo connection pool and timeouts.
    - Overlap-safe execution tracking.
    - Explicit wait_for cycles to satisfy Kubernetes termination periods.
    """
    def __init__(self):
        # Configure robust connection pool and timeouts to prevent socket hangs
        self.db_client = AsyncMongoClient(
            settings.MONGODB_URI,
            maxPoolSize=50,
            minPoolSize=5,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=10000,
            socketTimeoutMS=30000,
        )
        self.poller = LandingAIPoller(self.db_client)
        self.should_exit = False

    async def _health_check(self):
        """Startup health validation."""
        logger.info("Performing startup health check for Poller...")
        try:
            # Mongo check
            await self.db_client.admin.command("ping")
            logger.info("MongoDB connection health check passed.")
            
            # Ensure indexes are defined for horizontal scalability safety
            await self.poller.ensure_indexes()
        except Exception as e:
            logger.error(f"Startup health check FAILED: {e}")
            raise

    async def _execute_poll_cycle(self) -> tuple[dict, bool]:
        """
        Executes a single concurrent poll cycle under timeout limits.
        Returns a tuple of (stats_dict, is_failure_flag).
        """
        try:
            stats = await asyncio.wait_for(
                self.poller.poll_once(),
                timeout=300.0
            )
            return stats, False
        except asyncio.TimeoutError:
            logger.error("Poll cycle timed out (exceeded 300 seconds limit)")
            return {}, True
        except asyncio.CancelledError:
            logger.info("Cycle cancelled during poller execution.")
            raise
        except Exception as e:
            logger.error(f"Error during poll cycle: {e}")
            return {}, True

    def _log_metrics(self, stats: dict, elapsed: float):
        """Logs cycle execution statistics for metrics monitoring."""
        if not stats:
            return
        logger.info(
            "Poll cycle complete",
            extra={
                "duration": elapsed,
                "processed": stats.get("processed", 0),
                "completed": stats.get("completed", 0),
                "failed": stats.get("failed", 0),
                "transient": stats.get("transient", 0),
                "timeout": stats.get("timeout", 0),
            }
        )

    def _calculate_sleep_time(self, failure_count: int, elapsed: float, interval_seconds: float) -> float:
        """Determines the appropriate interval to sleep, applying exponential backoff if needed."""
        base_sleep = max(0.1, interval_seconds - elapsed)
        if failure_count > 0:
            backoff_sleep = min(60.0, 2.0 ** failure_count)
            sleep_time = max(base_sleep, backoff_sleep)
            logger.warning(f"Consecutive cycle failures: {failure_count}. Backing off sleep to {sleep_time:.1f}s.")
            return sleep_time
        return base_sleep

    async def _sleep_non_blocking(self, sleep_time: float):
        """Performs non-blocking sleeps in small increments, allowing instant signal shutdown response."""
        for _ in range(int(sleep_time * 10)):
            if self.should_exit:
                break
            await asyncio.sleep(0.1)

    async def run(self, interval_seconds: float = 5.0):
        """Main Loop."""
        await self._health_check()
        logger.info(f"Landing AI Poller Worker started (polling interval = {interval_seconds}s)")

        failure_count = 0

        while not self.should_exit:
            started = asyncio.get_running_loop().time()
            
            stats, is_failure = await self._execute_poll_cycle()
            if is_failure:
                failure_count += 1
            else:
                failure_count = 0

            elapsed = asyncio.get_running_loop().time() - started

            self._log_metrics(stats, elapsed)
            sleep_time = self._calculate_sleep_time(failure_count, elapsed, interval_seconds)
            await self._sleep_non_blocking(sleep_time)

        logger.info("Cleaning up connections and shutting down...")
        await self.poller.close()
        await self.db_client.close()
        logger.info("Poller Worker shutdown complete.")

    def stop(self, *args):
        logger.info("Stop signal received. Initiating graceful shutdown...")
        self.should_exit = True

async def main():
    worker = ProductionLandingAIPollerWorker()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:
            logger.warning(f"add_signal_handler not implemented for signal {sig.name or sig}, fallback used.")

    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())
