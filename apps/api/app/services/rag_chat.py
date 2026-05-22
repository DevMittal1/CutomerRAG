from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Sequence
from uuid import uuid4

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import UpdateOne
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings
from app.logging_config import get_logger
from app.logging_config import request_id_var
from app.schemas import ChatStreamRequest

logger = get_logger("app.services.rag_chat")

try:
    import cohere
except ImportError:  # pragma: no cover - optional dependency guard
    cohere = None

try:
    from fastembed import SparseTextEmbedding
except ImportError:  # pragma: no cover - optional dependency guard
    SparseTextEmbedding = None

from google import genai
from google.genai import types
from qdrant_client import AsyncQdrantClient, models


SYSTEM_INSTRUCTION = """You are the production CustomerRAG assistant.

You must answer using retrieved context from the retrieve_context tool response.
If the retrieved context is insufficient, say that clearly.
Do not invent facts, policies, or citations.
Prefer concise, direct answers.
When useful, cite sources inline using [1], [2], etc. matching the provided references.
"""


@dataclass(slots=True)
class RetrievedChunk:
    reference_id: int
    document_id: str
    file_key: str | None
    title: str
    source: str
    page: int | None
    score: float
    text: str


@dataclass(slots=True)
class RetrievalResult:
    query: str
    context: str
    chunks: list[RetrievedChunk]
    before_rerank_chunks: list[RetrievedChunk]
    after_rerank_chunks: list[RetrievedChunk]
    retrieval_status: str


class ConfigurationError(RuntimeError):
    pass


class ProductionRAGChatService:
    def __init__(self) -> None:
        self._genai_client: Any | None = None
        self._qdrant: AsyncQdrantClient | None = None
        self._cohere_client: Any | None = None
        self._sparse_model: Any | None = None
        self._init_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def close(self) -> None:
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        if self._genai_client is not None:
            await self._genai_client.aclose()
            self._genai_client = None
        if self._qdrant is not None:
            close_qdrant = getattr(self._qdrant, "close", None)
            if callable(close_qdrant):
                maybe_coro = close_qdrant()
                if asyncio.iscoroutine(maybe_coro):
                    await maybe_coro
            self._qdrant = None
        self._cohere_client = None
        self._sparse_model = None

    async def _ensure_clients(self) -> None:
        if self._genai_client is not None and self._qdrant is not None:
            return

        async with self._init_lock:
            if self._genai_client is None:
                if not settings.effective_gemini_api_key:
                    raise ConfigurationError(
                        "GEMINI_API_KEY or GOOGLE_API_KEY must be configured for chat."
                    )
                client = genai.Client(api_key=settings.effective_gemini_api_key)
                self._genai_client = client.aio

            if self._qdrant is None:
                self._qdrant = AsyncQdrantClient(
                    url=settings.QDRANT_URL,
                    api_key=settings.QDRANT_API_KEY,
                    prefer_grpc=settings.QDRANT_PREFER_GRPC,
                    timeout=settings.RAG_QDRANT_TIMEOUT_SECONDS,
                )

            if self._cohere_client is None and settings.COHERE_API_KEY:
                if cohere is None:
                    raise ConfigurationError(
                        "The 'cohere' package is required for reranking but is not installed."
                    )
                self._cohere_client = cohere.AsyncClientV2(
                    api_key=settings.COHERE_API_KEY
                )

            if (
                self._sparse_model is None
                and settings.RAG_ENABLE_SPARSE_RETRIEVAL
                and settings.QDRANT_SPARSE_VECTOR_NAME
            ):
                if SparseTextEmbedding is None:
                    logger.warning(
                        "Sparse retrieval requested but fastembed is not installed. Dense-only fallback will be used."
                    )
                else:
                    self._sparse_model = SparseTextEmbedding(
                        model_name=settings.RAG_SPARSE_MODEL_NAME
                    )

    async def stream_chat(
        self,
        *,
        payload: ChatStreamRequest,
        current_user: dict[str, Any],
        db: Any,
        request: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        await self._ensure_clients()

        user_id = str(current_user["_id"])
        contents = self._build_conversation_contents(payload)
        latest_user_message = payload.messages[-1].content
        trace_group_id = str(uuid4())

        planning_response = await self._run_with_retries(
            lambda: self._plan_tool_call(contents=contents),
            label="tool planning",
        )
        function_call = self._extract_retrieve_context_call(planning_response)
        retrieval_query = (
            function_call.args.get("query")
            if function_call and function_call.args and function_call.args.get("query")
            else latest_user_message
        )

        retrieval = await self.retrieve_context(
            query=retrieval_query,
            owner_user_id=current_user["_id"],
            document_ids=payload.document_ids,
            db=db,
        )

        yield {
            "event": "retrieval",
            "data": {
                "query": retrieval_query,
                "citations": [
                    {
                        "reference_id": chunk.reference_id,
                        "document_id": chunk.document_id,
                        "file_key": chunk.file_key,
                        "title": chunk.title,
                        "source": chunk.source,
                        "page": chunk.page,
                        "score": chunk.score,
                        "snippet": self._truncate(chunk.text, 280),
                    }
                    for chunk in retrieval.chunks
                ],
                "retrieved_count": len(retrieval.chunks),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        }

        tool_call_content = self._tool_call_content(function_call, retrieval_query)
        tool_response_content = types.Content(
            role="user",
            parts=[
                types.Part.from_function_response(
                    name="retrieve_context",
                    response={
                        "output": {
                            "query": retrieval_query,
                            "context": retrieval.context,
                            "citations": [
                                {
                                    "reference_id": chunk.reference_id,
                                    "document_id": chunk.document_id,
                                    "title": chunk.title,
                                    "source": chunk.source,
                                    "page": chunk.page,
                                    "score": chunk.score,
                                }
                                for chunk in retrieval.chunks
                            ],
                        }
                    },
                )
            ],
        )

        final_contents = [*contents, tool_call_content, tool_response_content]
        final_text_parts: list[str] = []
        client_disconnected = False

        stream = await self._run_with_retries(
            lambda: self._stream_grounded_answer(contents=final_contents),
            label="grounded generation stream",
        )

        async for chunk in stream:
            if await request.is_disconnected():
                logger.info("Client disconnected during chat stream", extra={"user_id": user_id})
                client_disconnected = True
                break
            text = getattr(chunk, "text", None)
            if not text:
                continue
            final_text_parts.append(text)
            yield {"event": "token", "data": {"text": text}}

        final_answer = "".join(final_text_parts).strip()
        yield {
            "event": "done",
            "data": {
                "text": final_answer,
                "citations": [
                    {
                        "reference_id": chunk.reference_id,
                        "document_id": chunk.document_id,
                        "file_key": chunk.file_key,
                        "title": chunk.title,
                        "source": chunk.source,
                        "page": chunk.page,
                        "score": chunk.score,
                        "snippet": self._truncate(chunk.text, 280),
                    }
                    for chunk in retrieval.chunks
                ],
            },
        }

        if settings.RAG_EVAL_TRACE_ENABLED:
            self._schedule_background_task(
                self._persist_evaluation_traces(
                    db=db,
                    payload=payload,
                    current_user=current_user,
                    retrieval=retrieval,
                    answer=final_answer,
                    trace_group_id=trace_group_id,
                    completion_status="disconnected" if client_disconnected else "completed",
                ),
                label="rag evaluation trace persistence",
                extra={"user_id": user_id, "trace_group_id": trace_group_id},
            )

    def _build_conversation_contents(
        self, payload: ChatStreamRequest
    ) -> list[types.Content]:
        contents: list[types.Content] = []
        for message in payload.messages:
            contents.append(
                types.Content(
                    role=message.role,
                    parts=[types.Part(text=message.content)],
                )
            )
        return contents

    async def _plan_tool_call(
        self, *, contents: Sequence[types.Content]
    ) -> types.GenerateContentResponse:
        config = types.GenerateContentConfig(
            temperature=0,
            candidate_count=1,
            system_instruction=SYSTEM_INSTRUCTION,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name="retrieve_context",
                            description=(
                                "Retrieve relevant grounded context from the vector database "
                                "for the current user request."
                            ),
                            parameters_json_schema={
                                "type": "object",
                                "properties": {
                                    "query": {
                                        "type": "string",
                                        "description": "The search query to run against the retrieval system.",
                                    }
                                },
                                "required": ["query"],
                                "additionalProperties": False,
                            },
                        )
                    ]
                )
            ],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="ANY",
                    allowed_function_names=["retrieve_context"],
                )
            ),
        )

        return await asyncio.wait_for(
            self._genai_client.models.generate_content(
                model=settings.GEMINI_CHAT_MODEL,
                contents=list(contents),
                config=config,
            ),
            timeout=settings.RAG_REQUEST_TIMEOUT_SECONDS,
        )

    async def _stream_grounded_answer(
        self, *, contents: Sequence[types.Content]
    ) -> AsyncIterator[types.GenerateContentResponse]:
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            candidate_count=1,
            temperature=settings.RAG_GENERATION_TEMPERATURE,
            max_output_tokens=settings.RAG_GENERATION_MAX_OUTPUT_TOKENS,
        )
        return await asyncio.wait_for(
            self._genai_client.models.generate_content_stream(
                model=settings.GEMINI_CHAT_MODEL,
                contents=list(contents),
                config=config,
            ),
            timeout=settings.RAG_REQUEST_TIMEOUT_SECONDS,
        )

    def _extract_retrieve_context_call(
        self, response: types.GenerateContentResponse
    ) -> types.FunctionCall | None:
        function_calls = response.function_calls or []
        for call in function_calls:
            if call.name == "retrieve_context":
                return call
        return None

    def _tool_call_content(
        self,
        function_call: types.FunctionCall | None,
        retrieval_query: str,
    ) -> types.Content:
        call = function_call or types.FunctionCall(
            name="retrieve_context",
            args={"query": retrieval_query},
        )
        return types.Content(
            role="model",
            parts=[
                types.Part.from_function_call(
                    name=call.name or "retrieve_context",
                    args=call.args or {"query": retrieval_query},
                )
            ],
        )

    async def retrieve_context(
        self,
        *,
        query: str,
        owner_user_id: ObjectId,
        document_ids: list[str] | None,
        db: Any,
    ) -> RetrievalResult:
        allowed_document_ids = await self._resolve_authorized_document_ids(
            owner_user_id=owner_user_id,
            requested_document_ids=document_ids,
            db=db,
        )

        if not allowed_document_ids:
            return RetrievalResult(
                query=query,
                context="No indexed documents are currently available for this user.",
                chunks=[],
                before_rerank_chunks=[],
                after_rerank_chunks=[],
                retrieval_status="no_documents",
            )

        dense_task = asyncio.create_task(self._dense_embed(query))
        sparse_task = asyncio.create_task(self._sparse_embed(query))
        dense_vector, sparse_vector = await asyncio.gather(dense_task, sparse_task)

        query_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchAny(any=allowed_document_ids),
                )
            ]
        )

        points = await self._query_qdrant(
            dense_vector=dense_vector,
            sparse_vector=sparse_vector,
            query_filter=query_filter,
        )
        chunks = self._deduplicate_points(points)
        reranked_chunks = await self._rerank(query=query, chunks=chunks)
        final_chunks = reranked_chunks[: settings.RAG_MAX_CONTEXT_CHUNKS]

        if not final_chunks:
            return RetrievalResult(
                query=query,
                context="No relevant indexed context was found for the request.",
                chunks=[],
                before_rerank_chunks=chunks,
                after_rerank_chunks=reranked_chunks,
                retrieval_status="no_results",
            )

        context_blocks = []
        for chunk in final_chunks:
            context_blocks.append(
                "\n".join(
                    [
                        f"[DOCUMENT {chunk.reference_id}]",
                        "",
                        f"TITLE:\n{chunk.title}",
                        "",
                        f"SOURCE:\n{chunk.source}",
                        "",
                        f"PAGE:\n{chunk.page if chunk.page is not None else 'unknown'}",
                        "",
                        f"CONTENT:\n{chunk.text}",
                    ]
                )
            )

        return RetrievalResult(
            query=query,
            context="\n\n".join(context_blocks),
            chunks=final_chunks,
            before_rerank_chunks=chunks,
            after_rerank_chunks=reranked_chunks,
            retrieval_status="completed",
        )

    async def _resolve_authorized_document_ids(
        self,
        *,
        owner_user_id: ObjectId,
        requested_document_ids: list[str] | None,
        db: Any,
    ) -> list[str]:
        query: dict[str, Any] = {
            "user_id": owner_user_id,
            "status": "completed",
            "embedding_status": "completed",
        }

        explicit_object_ids: list[ObjectId] = []
        if requested_document_ids:
            for raw_id in requested_document_ids:
                try:
                    explicit_object_ids.append(ObjectId(raw_id))
                except InvalidId as exc:
                    raise ValueError(f"Invalid document id: {raw_id}") from exc
            query["_id"] = {"$in": explicit_object_ids}

        limit = len(explicit_object_ids) if explicit_object_ids else settings.RAG_MAX_FILTER_DOCUMENT_IDS
        cursor = (
            db.documents.find(query, {"_id": 1})
            .sort("updated_at", -1)
            .limit(limit)
        )
        documents = await cursor.to_list(length=limit)
        return [str(doc["_id"]) for doc in documents]

    async def _dense_embed(self, text: str) -> list[float]:
        async for attempt in self._retry_loop("dense embed"):
            with attempt:
                response = await asyncio.wait_for(
                    self._genai_client.models.embed_content(
                        model=settings.RAG_QUERY_EMBEDDING_MODEL,
                        contents=text,
                        config=types.EmbedContentConfig(
                            task_type="RETRIEVAL_QUERY",
                            output_dimensionality=settings.RAG_QUERY_EMBEDDING_OUTPUT_DIMENSIONALITY,
                        ),
                    ),
                    timeout=settings.RAG_REQUEST_TIMEOUT_SECONDS,
                )
                return list(response.embeddings[0].values)
        raise RuntimeError("Dense embedding retries were exhausted.")

    async def _sparse_embed(self, text: str) -> models.SparseVector | None:
        if self._sparse_model is None or not settings.QDRANT_SPARSE_VECTOR_NAME:
            return None

        embedding = await asyncio.to_thread(self._embed_sparse_sync, text)
        return models.SparseVector(
            indices=embedding["indices"],
            values=embedding["values"],
        )

    def _embed_sparse_sync(self, text: str) -> dict[str, list[float] | list[int]]:
        sparse_embedding = next(self._sparse_model.embed(text))
        return {
            "indices": sparse_embedding.indices.tolist(),
            "values": sparse_embedding.values.tolist(),
        }

    async def _query_qdrant(
        self,
        *,
        dense_vector: list[float],
        sparse_vector: models.SparseVector | None,
        query_filter: models.Filter,
    ) -> list[Any]:
        async for attempt in self._retry_loop("qdrant query"):
            with attempt:
                if sparse_vector is not None:
                    try:
                        response = await asyncio.wait_for(
                            self._qdrant.query_points(
                                collection_name=settings.QDRANT_COLLECTION_NAME,
                                prefetch=[
                                    models.Prefetch(
                                        query=dense_vector,
                                        using=settings.QDRANT_DENSE_VECTOR_NAME,
                                        limit=settings.RAG_RETRIEVE_LIMIT_DENSE,
                                        filter=query_filter,
                                    ),
                                    models.Prefetch(
                                        query=sparse_vector,
                                        using=settings.QDRANT_SPARSE_VECTOR_NAME,
                                        limit=settings.RAG_RETRIEVE_LIMIT_SPARSE,
                                        filter=query_filter,
                                    ),
                                ],
                                query=models.FusionQuery(fusion=models.Fusion.RRF),
                                limit=settings.RAG_RETRIEVE_LIMIT_FINAL,
                                with_payload=True,
                                with_vectors=False,
                            ),
                            timeout=settings.RAG_REQUEST_TIMEOUT_SECONDS,
                        )
                    except Exception:
                        logger.exception(
                            "Hybrid query failed; falling back to dense-only retrieval."
                        )
                        response = await asyncio.wait_for(
                            self._qdrant.query_points(
                                collection_name=settings.QDRANT_COLLECTION_NAME,
                                query=dense_vector,
                                using=settings.QDRANT_DENSE_VECTOR_NAME,
                                query_filter=query_filter,
                                limit=settings.RAG_RETRIEVE_LIMIT_FINAL,
                                with_payload=True,
                                with_vectors=False,
                            ),
                            timeout=settings.RAG_REQUEST_TIMEOUT_SECONDS,
                        )
                else:
                    response = await asyncio.wait_for(
                        self._qdrant.query_points(
                            collection_name=settings.QDRANT_COLLECTION_NAME,
                            query=dense_vector,
                            using=settings.QDRANT_DENSE_VECTOR_NAME,
                            query_filter=query_filter,
                            limit=settings.RAG_RETRIEVE_LIMIT_FINAL,
                            with_payload=True,
                            with_vectors=False,
                        ),
                        timeout=settings.RAG_REQUEST_TIMEOUT_SECONDS,
                    )
                return list(response.points)
        raise RuntimeError("Qdrant query retries were exhausted.")

    def _deduplicate_points(self, points: Sequence[Any]) -> list[RetrievedChunk]:
        seen: set[str] = set()
        unique_chunks: list[RetrievedChunk] = []

        for point in points:
            payload = point.payload or {}
            text = str(payload.get("text") or payload.get("content") or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)

            metadata = payload.get("metadata") or {}
            file_key = payload.get("file_key")
            title = (
                metadata.get("title")
                or metadata.get("filename")
                or file_key
                or f"Document {payload.get('document_id', 'unknown')}"
            )
            source = str(
                payload.get("source")
                or payload.get("source_url")
                or file_key
                or "unknown"
            )
            unique_chunks.append(
                RetrievedChunk(
                    reference_id=len(unique_chunks) + 1,
                    document_id=str(payload.get("document_id", "")),
                    file_key=str(file_key) if file_key is not None else None,
                    title=str(title),
                    source=source,
                    page=payload.get("page"),
                    score=float(point.score or 0.0),
                    text=text,
                )
            )

        return unique_chunks

    async def _rerank(
        self, *, query: str, chunks: Sequence[RetrievedChunk]
    ) -> list[RetrievedChunk]:
        if not chunks:
            return []

        if self._cohere_client is None:
            if settings.RAG_REQUIRE_COHERE_RERANK:
                raise ConfigurationError(
                    "COHERE_API_KEY is required because RAG_REQUIRE_COHERE_RERANK is enabled."
                )
            return list(chunks)

        documents = [
            {
                "text": chunk.text,
                "title": chunk.title,
                "source": chunk.source,
            }
            for chunk in chunks
        ]

        async for attempt in self._retry_loop("cohere rerank"):
            with attempt:
                response = await asyncio.wait_for(
                    self._cohere_client.rerank(
                        model=settings.COHERE_RERANK_MODEL,
                        query=query,
                        documents=documents,
                        top_n=min(settings.RAG_RERANK_TOP_N, len(documents)),
                    ),
                    timeout=settings.RAG_REQUEST_TIMEOUT_SECONDS,
                )
                ranked: list[RetrievedChunk] = []
                for item in response.results:
                    chunk = chunks[item.index]
                    ranked.append(
                        RetrievedChunk(
                            reference_id=chunk.reference_id,
                            document_id=chunk.document_id,
                            file_key=chunk.file_key,
                            title=chunk.title,
                            source=chunk.source,
                            page=chunk.page,
                            score=float(item.relevance_score),
                            text=chunk.text,
                        )
                    )
                return ranked
        raise RuntimeError("Cohere rerank retries were exhausted.")

    async def _run_with_retries(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        label: str,
    ) -> Any:
        async for attempt in self._retry_loop(label):
            with attempt:
                return await operation()
        raise RuntimeError(f"{label} retries were exhausted.")

    def _retry_loop(self, label: str) -> AsyncRetrying:
        return AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception_type(Exception),
            reraise=True,
            before_sleep=lambda retry_state: logger.warning(
                "Retrying %s after error: %s",
                label,
                retry_state.outcome.exception() if retry_state.outcome else "unknown",
            ),
        )

    def _truncate(self, text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 3].rstrip() + "..."

    def _schedule_background_task(
        self,
        coro: Awaitable[Any],
        *,
        label: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _done_callback(done_task: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done_task)
            if done_task.cancelled():
                return
            exc = done_task.exception()
            if exc is not None:
                logger.error(
                    "Background task failed: %s",
                    label,
                    extra=extra,
                    exc_info=exc,
                )

        task.add_done_callback(_done_callback)

    async def _persist_evaluation_traces(
        self,
        *,
        db: Any,
        payload: ChatStreamRequest,
        current_user: dict[str, Any],
        retrieval: RetrievalResult,
        answer: str,
        trace_group_id: str,
        completion_status: str,
    ) -> None:
        trace_docs = self._build_trace_documents(
            payload=payload,
            current_user=current_user,
            retrieval=retrieval,
            answer=answer,
            trace_group_id=trace_group_id,
            completion_status=completion_status,
        )
        if not trace_docs:
            return

        collection = db[settings.RAG_EVAL_TRACE_COLLECTION]
        operations = [
            UpdateOne(
                {"trace_group_id": doc["trace_group_id"], "trace_type": doc["trace_type"]},
                {"$setOnInsert": doc},
                upsert=True,
            )
            for doc in trace_docs
        ]
        await collection.bulk_write(operations, ordered=False)

    def _build_trace_documents(
        self,
        *,
        payload: ChatStreamRequest,
        current_user: dict[str, Any],
        retrieval: RetrievalResult,
        answer: str,
        trace_group_id: str,
        completion_status: str,
    ) -> list[dict[str, Any]]:
        latest_user_message = payload.messages[-1].content
        now = datetime.now(timezone.utc)
        base_doc = {
            "trace_group_id": trace_group_id,
            "request_id": request_id_var.get(),
            "user_id": str(current_user["_id"]),
            "document_ids": payload.document_ids or [],
            "query": retrieval.query,
            "latest_user_message": latest_user_message,
            "answer": answer,
            "reference_answer": None,
            "retrieval_status": retrieval.retrieval_status,
            "completion_status": completion_status,
            "eval_status": "pending",
            "eval_attempts": 0,
            "created_at": now,
            "updated_at": now,
            "lease_expires_at": None,
            "metrics": {},
            "metric_errors": {},
        }
        before_chunks = self._serialize_trace_chunks(retrieval.before_rerank_chunks)
        after_chunks = self._serialize_trace_chunks(retrieval.after_rerank_chunks)
        return [
            {
                **base_doc,
                "trace_type": "before_rerank",
                "retrieved_contexts": before_chunks,
                "retrieved_context_count": len(before_chunks),
            },
            {
                **base_doc,
                "trace_type": "after_rerank",
                "retrieved_contexts": after_chunks,
                "retrieved_context_count": len(after_chunks),
            },
        ]

    def _serialize_trace_chunks(
        self,
        chunks: Sequence[RetrievedChunk],
    ) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        max_contexts = max(1, settings.RAG_EVAL_TRACE_MAX_CONTEXTS)
        max_chars = max(32, settings.RAG_EVAL_TRACE_MAX_CONTEXT_CHARS)
        for chunk in chunks[:max_contexts]:
            serialized.append(
                {
                    "reference_id": chunk.reference_id,
                    "document_id": chunk.document_id,
                    "file_key": chunk.file_key,
                    "title": chunk.title,
                    "source": chunk.source,
                    "page": chunk.page,
                    "score": chunk.score,
                    "text": self._truncate(chunk.text, max_chars),
                }
            )
        return serialized


rag_chat_service = ProductionRAGChatService()
