import asyncio
import signal
import time
from pymongo import AsyncMongoClient
from app.config import settings
from app.utils.logging import setup_worker_logging, get_worker_logger
from app.poller import LandingAIPoller

setup_worker_logging()
logger = get_worker_logger("rag_worker.poll_main")

class LandingAIPollWorker:
    """
    Dedicated Worker for polling Landing AI job status.
    Keeps the ingestion worker focused on high-throughput submissions.
    """
    def __init__(self):
        self.db_client = AsyncMongoClient(settings.MONGODB_URI)
        self.poller = LandingAIPoller(self.db_client)
        self.should_exit = False

    async def run(self):
        logger.info("Landing AI Poll Worker started")
        
        while not self.should_exit:
            try:
                await self.poller.poll_once()
            except Exception as e:
                logger.error(f"Error in poll cycle: {e}")
            
            # Poll every 10 seconds (tuneable)
            await asyncio.sleep(10)

        await self.db_client.close()
        logger.info("Poll Worker shutdown complete.")

    def stop(self, *args):
        self.should_exit = True

async def main():
    worker = LandingAIPollWorker()
    
    # OS Signal Handling
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_event_loop().add_signal_handler(sig, worker.stop)
        
    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())
