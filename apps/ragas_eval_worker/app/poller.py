from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from numbers import Number
from typing import Any
from uuid import uuid4

from google import genai
from pymongo import AsyncMongoClient, ReturnDocument

from .config import settings
from .utils.logging import get_worker_logger

logger = get_worker_logger("worker.ragas_eval")

from ragas import SingleTurnSample
from ragas.embeddings.base import embedding_factory
from ragas.llms import llm_factory
from ragas.metrics import (
    ContextRecall,
    Faithfulness,
    LLMContextPrecisionWithoutReference,
    ResponseRelevancy,
)


class RagasEvaluationPoller:
    def __init__(self, db_client: AsyncMongoClient):
        self.db_client = db_client
        self.db = self.db_client[settings.MONGODB_DB_NAME]
        self.worker_id = str(uuid4())
        self._llm = None
        self._embeddings = None
        self._metrics: dict[str, Any] | None = None

    async def close(self):
        return None

    async def ensure_indexes(self):
        collection = self.db[settings.RAG_EVAL_TRACE_COLLECTION]
        await collection.create_index([("eval_status", 1), ("lease_expires_at", 1), ("created_at", 1)])
        await collection.create_index([("trace_group_id", 1), ("trace_type", 1)], unique=True)
        await collection.create_index([("user_id", 1), ("created_at", -1)])

    async def poll_once(self) -> dict[str, int]:
        stats = {"claimed": 0, "completed": 0, "failed": 0, "skipped": 0}
        batch_size = max(1, settings.RAGAS_EVAL_BATCH_SIZE)
        for _ in range(batch_size):
            trace = await self._claim_next_trace()
            if trace is None:
                break

            stats["claimed"] += 1
            try:
                outcome = await self._evaluate_trace(trace)
                if outcome["status"] == "completed":
                    stats["completed"] += 1
                else:
                    stats["skipped"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                stats["failed"] += 1
                await self._mark_failed(trace["_id"], exc)
        return stats

    async def _claim_next_trace(self) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        lease_expires_at = now + timedelta(seconds=settings.RAGAS_EVAL_LOCK_TIMEOUT_SECONDS)
        return await self.db[settings.RAG_EVAL_TRACE_COLLECTION].find_one_and_update(
            {
                "$or": [
                    {"eval_status": "pending"},
                    {
                        "eval_status": "processing",
                        "lease_expires_at": {"$lt": now},
                    },
                ],
                "eval_attempts": {"$lt": settings.RAGAS_EVAL_MAX_ATTEMPTS},
            },
            {
                "$set": {
                    "eval_status": "processing",
                    "lease_expires_at": lease_expires_at,
                    "eval_started_at": now,
                    "eval_worker_id": self.worker_id,
                    "updated_at": now,
                },
                "$inc": {"eval_attempts": 1},
            },
            sort=[("created_at", 1)],
            return_document=ReturnDocument.AFTER,
        )

    async def _evaluate_trace(self, trace: dict[str, Any]) -> dict[str, Any]:
        response = str(trace.get("answer") or "").strip()
        contexts = [
            str(context.get("text") or "").strip()
            for context in trace.get("retrieved_contexts", [])
            if str(context.get("text") or "").strip()
        ]
        if not response:
            await self._mark_skipped(trace["_id"], "empty_answer")
            return {"status": "skipped"}
        if not contexts:
            await self._mark_skipped(trace["_id"], "empty_contexts")
            return {"status": "skipped"}

        sample_kwargs = {
            "user_input": str(trace.get("query") or ""),
            "response": response,
            "retrieved_contexts": contexts,
        }
        reference_answer = str(trace.get("reference_answer") or "").strip()
        if reference_answer:
            sample_kwargs["reference"] = reference_answer
        sample = SingleTurnSample(**sample_kwargs)

        metrics = await self._get_metrics(include_reference_metrics=bool(reference_answer))
        scores: dict[str, float | None] = {}
        metric_errors: dict[str, str] = {}
        for metric_name, metric in metrics.items():
            try:
                result = await metric.single_turn_ascore(sample)
                scores[metric_name] = self._normalize_score(result)
            except Exception as exc:
                metric_errors[metric_name] = f"{exc.__class__.__name__}: {exc}"

        now = datetime.now(timezone.utc)
        await self.db[settings.RAG_EVAL_TRACE_COLLECTION].update_one(
            {"_id": trace["_id"]},
            {
                "$set": {
                    "eval_status": "completed",
                    "metrics": scores,
                    "metric_errors": metric_errors,
                    "eval_completed_at": now,
                    "lease_expires_at": None,
                    "updated_at": now,
                }
            },
        )
        return {"status": "completed"}

    async def _get_metrics(self, *, include_reference_metrics: bool) -> dict[str, Any]:
        await self._ensure_ragas_clients()
        assert self._metrics is not None
        metrics = {
            "context_precision": self._metrics["context_precision"],
            "faithfulness": self._metrics["faithfulness"],
            "response_relevancy": self._metrics["response_relevancy"],
        }
        if include_reference_metrics:
            metrics["context_recall"] = self._metrics["context_recall"]
        return metrics

    async def _ensure_ragas_clients(self):
        if self._metrics is not None:
            return
        if not settings.effective_google_api_key:
            raise RuntimeError("GEMINI_API_KEY or GOOGLE_API_KEY must be configured for RAGAS evaluation.")

        client = genai.Client(api_key=settings.effective_google_api_key)
        self._llm = llm_factory(
            settings.RAGAS_EVAL_MODEL,
            provider="google",
            client=client,
        )
        self._embeddings = embedding_factory(
            provider="google",
            model=settings.RAGAS_EVAL_EMBEDDING_MODEL,
            client=client,
        )
        self._metrics = {
            "context_precision": LLMContextPrecisionWithoutReference(llm=self._llm),
            "faithfulness": Faithfulness(llm=self._llm),
            "response_relevancy": ResponseRelevancy(
                llm=self._llm,
                embeddings=self._embeddings,
            ),
            "context_recall": ContextRecall(llm=self._llm),
        }

    async def _mark_failed(self, trace_id: Any, exc: Exception):
        now = datetime.now(timezone.utc)
        await self.db[settings.RAG_EVAL_TRACE_COLLECTION].update_one(
            {"_id": trace_id},
            {
                "$set": {
                    "eval_status": "failed",
                    "eval_error": f"{exc.__class__.__name__}: {exc}",
                    "eval_completed_at": now,
                    "lease_expires_at": None,
                    "updated_at": now,
                }
            },
        )

    async def _mark_skipped(self, trace_id: Any, reason: str):
        now = datetime.now(timezone.utc)
        await self.db[settings.RAG_EVAL_TRACE_COLLECTION].update_one(
            {"_id": trace_id},
            {
                "$set": {
                    "eval_status": "skipped",
                    "eval_skip_reason": reason,
                    "eval_completed_at": now,
                    "lease_expires_at": None,
                    "updated_at": now,
                }
            },
        )

    def _normalize_score(self, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, Number):
            return float(value)
        metric_value = getattr(value, "value", None)
        if isinstance(metric_value, Number):
            return float(metric_value)
        return None
