import asyncio
import httpx
from datetime import datetime, timezone
from bson import ObjectId
from pymongo import AsyncMongoClient
from app.config import settings
from app.utils.logging import get_worker_logger

logger = get_worker_logger("worker.poller")

class LandingAIPoller:
    """
    Polls Landing AI for job status and processes results.
    """
    def __init__(self, db_client: AsyncMongoClient):
        self.db_client = db_client
        self.db = self.db_client[settings.MONGODB_DB_NAME]
        self.headers = {
            "Authorization": f"Bearer {settings.LANDING_AI_API_KEY}"
        }

    async def check_job_status(self, job_id: str) -> dict:
        """
        Polls Landing AI for a single job status.
        """
        url = f"{settings.LANDING_AI_BASE_URL}/parse/jobs/{job_id}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self.headers)
            response.raise_for_status()
            return response.json()

    def prepare_chunk_documents(self, parse_result: dict, source_url: str, job_id: str):
        """
        Converts LandingAI chunks into MongoDB documents.
        """
        data = parse_result.get("data", {})
        chunks = data.get("chunks", [])
        docs = []

        for idx, chunk in enumerate(chunks):
            grounding = chunk.get("grounding", {})
            box = grounding.get("box", {})

            doc = {
                "_id": ObjectId(),
                "job_id": job_id,
                "source_url": source_url,
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
                    "landingai_version": parse_result.get("version"),
                    "received_at": parse_result.get("received_at"),
                },
                "created_at": datetime.now(timezone.utc),
            }
            docs.append(doc)
        return docs

    async def process_completed_job(self, doc: dict, result: dict):
        """
        Handles data extraction and storage for a completed Landing AI job.
        """
        job_id = doc["landing_ai_job_id"]
        file_key = doc["file_key"]
        
        logger.info(f"Processing completed job: {job_id} for {file_key}")
        
        # 1. Prepare chunks
        chunk_docs = self.prepare_chunk_documents(
            parse_result=result,
            source_url=file_key, # Simplified
            job_id=job_id
        )
        
        # 2. Store chunks
        if chunk_docs:
            await self.db[settings.COLL_CHUNKS].insert_many(chunk_docs)
            logger.info(f"Inserted {len(chunk_docs)} chunks for job {job_id}")
        
        # 3. Update document status
        await self.db[settings.COLL_DOCUMENTS].update_one(
            {"_id": doc["_id"]},
            {
                "$set": {
                    "status": "completed",
                    "chunk_count": len(chunk_docs),
                    "updated_at": datetime.now(timezone.utc)
                }
            }
        )

    async def poll_once(self):
        """
        Performs one cycle of polling for all pending jobs.
        """
        pending_docs = self.db[settings.COLL_DOCUMENTS].find({
            "status": "landing_ai_pending"
        })
        
        async for doc in pending_docs:
            job_id = doc["landing_ai_job_id"]
            try:
                result = await self.check_job_status(job_id)
                status = result.get("status")
                
                if status == "completed":
                    await self.process_completed_job(doc, result)
                elif status in ["failed", "error"]:
                    reason = result.get("failure_reason", "Unknown error")
                    logger.error(f"Job {job_id} failed: {reason}")
                    await self.db[settings.COLL_DOCUMENTS].update_one(
                        {"_id": doc["_id"]},
                        {
                            "$set": {
                                "status": "failed",
                                "error": reason,
                                "updated_at": datetime.now(timezone.utc)
                            }
                        }
                    )
                else:
                    progress = result.get("progress", 0)
                    logger.debug(f"Job {job_id} status: {status} ({progress*100:.1f}%)")
                    
            except Exception as e:
                logger.error(f"Error checking status for job {job_id}: {e}")
