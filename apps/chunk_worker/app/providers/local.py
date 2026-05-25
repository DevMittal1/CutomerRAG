import asyncio
import uuid
from datetime import datetime, timezone, timedelta

from llama_index.core.node_parser import HierarchicalNodeParser
from llama_index.readers.s3 import S3Reader
from pymongo import AsyncMongoClient, ReturnDocument
from redis.asyncio import Redis

from ..config import settings
from ..services.embeddings import trigger_inline_batch_embeddings_async
from observability.logging import get_worker_logger

logger = get_worker_logger("rag_chunk_worker.local")

class LocalChunkProvider:
    """
    Redis Stream consumer that owns the CPU-heavy internal chunking path.
    """
    def __init__(self, db_client: AsyncMongoClient, redis_client: Redis):
        self.worker_id = str(uuid.uuid4())
        self.consumer_name = f"chunk-worker-{self.worker_id}"
        self.db_client = db_client
        self.db = self.db_client[settings.MONGODB_DB_NAME]
        self.redis_client = redis_client
        self.node_parser = HierarchicalNodeParser.from_defaults(
            chunk_sizes=settings.CHUNK_SIZES
        )
        self.semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_TASKS)
        self.should_exit = False
        self.active_tasks: set[asyncio.Task] = set()
        self.reclaim_start_id = "0-0"

    async def ensure_indexes(self):
        await self.db[settings.COLL_DOCUMENTS].create_index("file_key", unique=True)
        await self.db[settings.COLL_CHUNKS].create_index(
            [("document_id", 1), ("chunk_index", 1)],
            unique=True,
        )

    async def ensure_consumer_group(self):
        try:
            await self.redis_client.xgroup_create(
                name=settings.REDIS_CHUNK_STREAM,
                groupname=settings.REDIS_CHUNK_CONSUMER_GROUP,
                id="0-0",
                mkstream=True,
            )
            logger.info("Created Redis consumer group for internal chunking")
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def run(self):
        await self.ensure_indexes()
        await self.ensure_consumer_group()
        reclaim_task = asyncio.create_task(self.reclaim_loop())
        logger.info(
            "Chunk worker started",
            extra={
                "consumer": self.consumer_name,
                "stream": settings.REDIS_CHUNK_STREAM,
                "group": settings.REDIS_CHUNK_CONSUMER_GROUP,
                "concurrency": settings.MAX_CONCURRENT_TASKS,
            },
        )

        try:
            while not self.should_exit:
                available_slots = max(
                    1,
                    settings.MAX_CONCURRENT_TASKS - len(self.active_tasks),
                )
                messages = await self.redis_client.xreadgroup(
                    groupname=settings.REDIS_CHUNK_CONSUMER_GROUP,
                    consumername=self.consumer_name,
                    streams={settings.REDIS_CHUNK_STREAM: ">"},
                    count=available_slots,
                    block=settings.REDIS_CHUNK_BLOCK_MS,
                )
                if not messages:
                    continue

                for _, stream_messages in messages:
                    for stream_id, fields in stream_messages:
                        task = asyncio.create_task(
                            self.process_message(stream_id, fields)
                        )
                        self.active_tasks.add(task)
                        task.add_done_callback(self.active_tasks.discard)
        finally:
            reclaim_task.cancel()
            await asyncio.gather(reclaim_task, return_exceptions=True)
            if self.active_tasks:
                await asyncio.gather(*self.active_tasks, return_exceptions=True)
            logger.info("Chunk worker local provider run finished")

    async def reclaim_loop(self):
        while not self.should_exit:
            try:
                result = await self.redis_client.xautoclaim(
                    name=settings.REDIS_CHUNK_STREAM,
                    groupname=settings.REDIS_CHUNK_CONSUMER_GROUP,
                    consumername=self.consumer_name,
                    min_idle_time=settings.REDIS_CHUNK_RECLAIM_IDLE_MS,
                    start_id=self.reclaim_start_id,
                    count=settings.REDIS_CHUNK_RECLAIM_BATCH_SIZE,
                )
                next_start_id, reclaimed_messages, *_ = result
                self.reclaim_start_id = next_start_id or "0-0"
                for stream_id, fields in reclaimed_messages:
                    if len(self.active_tasks) >= settings.MAX_CONCURRENT_TASKS:
                        break
                    task = asyncio.create_task(
                        self.process_message(stream_id, fields)
                    )
                    self.active_tasks.add(task)
                    task.add_done_callback(self.active_tasks.discard)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed to reclaim pending chunk messages")

            await asyncio.sleep(settings.REDIS_CHUNK_RECLAIM_INTERVAL_SECONDS)

    async def process_message(self, stream_id: str, fields: dict[str, str]):
        async with self.semaphore:
            key = fields["key"]
            bucket = fields["bucket"]
            claimed_document = await self.claim_document(key=key, stream_id=stream_id)
            if claimed_document is None:
                await self.ack(stream_id)
                return

            try:
                nodes = await self.load_and_chunk_document(bucket=bucket, key=key)
                await self.replace_chunks(document=claimed_document, nodes=nodes)
                embedding_fields = await self.submit_embeddings_for_document(
                    document=claimed_document,
                    nodes=nodes,
                )
                await self.db[settings.COLL_DOCUMENTS].update_one(
                    {"_id": claimed_document["_id"]},
                    {
                        "$set": {
                            "status": "completed",
                            "chunk_count": len(nodes),
                            "updated_at": datetime.now(timezone.utc),
                            **embedding_fields,
                        },
                        "$unset": {
                            "chunk_owner": "",
                            "chunk_claimed_at": "",
                            "chunk_error": "",
                        },
                    },
                )
                await self.ack(stream_id)
                logger.info(
                    "Chunked document successfully",
                    extra={
                        "key": key,
                        "stream_id": stream_id,
                        "chunk_count": len(nodes),
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.db[settings.COLL_DOCUMENTS].update_one(
                    {"_id": claimed_document["_id"]},
                    {
                        "$set": {
                            "status": "chunk_failed",
                            "chunk_error": str(exc),
                            "updated_at": datetime.now(timezone.utc),
                        },
                        "$unset": {
                            "chunk_owner": "",
                            "chunk_claimed_at": "",
                        },
                    },
                )
                logger.exception(
                    "Internal chunking failed",
                    extra={"key": key, "stream_id": stream_id},
                )

    async def claim_document(self, key: str, stream_id: str) -> dict | None:
        stale_before = datetime.now(timezone.utc) - timedelta(
            milliseconds=settings.REDIS_CHUNK_RECLAIM_IDLE_MS
        )
        return await self.db[settings.COLL_DOCUMENTS].find_one_and_update(
            {
                "file_key": key,
                "$or": [
                    {"status": "queued_for_chunking"},
                    {"status": "chunk_failed"},
                    {
                        "status": "chunking",
                        "chunk_claimed_at": {"$lt": stale_before},
                    },
                ],
            },
            {
                "$set": {
                    "status": "chunking",
                    "chunk_owner": self.consumer_name,
                    "chunk_claimed_at": datetime.now(timezone.utc),
                    "current_chunk_stream_id": stream_id,
                    "updated_at": datetime.now(timezone.utc),
                },
                "$inc": {"chunk_attempts": 1},
            },
            return_document=ReturnDocument.AFTER,
        )

    async def load_and_chunk_document(self, bucket: str, key: str):
        reader = S3Reader(
            bucket=bucket,
            key=key,
            aws_access_id=settings.AWS_ACCESS_KEY_ID,
            aws_access_secret=settings.AWS_SECRET_ACCESS_KEY,
        )
        documents = await reader.aload_data()
        return await self.node_parser.aget_nodes_from_documents(documents)

    async def replace_chunks(self, document: dict, nodes: list):
        document_id = document["_id"]
        await self.db[settings.COLL_CHUNKS].delete_many({"document_id": document_id})
        if not nodes:
            return

        now = datetime.now(timezone.utc)
        await self.db[settings.COLL_CHUNKS].insert_many(
            [
                {
                    "document_id": document_id,
                    "file_key": document["file_key"],
                    "chunk_index": idx,
                    "chunk_id": getattr(node, "node_id", None),
                    "content": node.text,
                    "metadata": dict(getattr(node, "metadata", {}) or {}),
                    "created_at": now,
                }
                for idx, node in enumerate(nodes)
            ],
            ordered=True,
        )

    async def submit_embeddings_for_document(self, document: dict, nodes: list) -> dict:
        now = datetime.now(timezone.utc)
        requests = []
        for idx, node in enumerate(nodes):
            chunk_text = getattr(node, "text", None)
            if not chunk_text:
                continue
            requests.append(
                {
                    "output_dimensionality": settings.GEMINI_EMBEDDING_OUTPUT_DIMENSIONALITY,
                    "content": {
                        "parts": [
                            {"text": f"task: search_document | text: {chunk_text}"}
                        ]
                    },
                    "metadata": {
                        "document_id": str(document["_id"]),
                        "file_key": document["file_key"],
                        "chunk_index": str(idx),
                        "chunk_id": getattr(node, "node_id", "") or "",
                    },
                }
            )

        if not requests:
            return {
                "embedding_status": "skipped",
                "embedding_error": "No chunk content available for embedding submission.",
                "embedding_updated_at": now,
            }

        if not settings.GOOGLE_API_KEY:
            return {
                "embedding_status": "skipped",
                "embedding_error": "GOOGLE_API_KEY is not configured.",
                "embedding_updated_at": now,
            }

        try:
            embedding_job_id = await trigger_inline_batch_embeddings_async(
                requests=requests,
                api_key=settings.GOOGLE_API_KEY,
                model=settings.GEMINI_EMBEDDING_MODEL,
                display_name=settings.GEMINI_EMBEDDING_DISPLAY_NAME,
                max_inline_bytes=settings.GEMINI_EMBEDDING_MAX_INLINE_BYTES,
            )
            return {
                "embedding_job_id": embedding_job_id,
                "embedding_status": "submitted",
                "embedding_model": settings.GEMINI_EMBEDDING_MODEL,
                "embedding_poll_attempts": 0,
                "embedding_submitted_at": now,
                "embedding_next_poll_at": now,
                "embedding_updated_at": now,
                "embedding_error": None,
            }
        except Exception as exc:
            logger.exception(
                "Embedding submission failed",
                extra={"file_key": document["file_key"]},
            )
            return {
                "embedding_status": "failed",
                "embedding_model": settings.GEMINI_EMBEDDING_MODEL,
                "embedding_poll_attempts": 0,
                "embedding_updated_at": now,
                "embedding_error": str(exc),
            }

    async def ack(self, stream_id: str):
        await self.redis_client.xack(
            settings.REDIS_CHUNK_STREAM,
            settings.REDIS_CHUNK_CONSUMER_GROUP,
            stream_id,
        )

    def stop(self):
        self.should_exit = True
