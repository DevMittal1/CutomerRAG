from __future__ import annotations

import logging
from typing import Sequence, TypedDict

from google import genai
from google.genai import types
from google.genai.errors import APIError

logger = logging.getLogger(__name__)


class InlineEmbeddingPart(TypedDict):
    text: str


class InlineEmbeddingContent(TypedDict):
    parts: list[InlineEmbeddingPart]


class InlineEmbeddingRequest(TypedDict, total=False):
    output_dimensionality: int
    content: InlineEmbeddingContent
    metadata: dict[str, str]


async def trigger_inline_batch_embeddings_async(
    *,
    requests: Sequence[InlineEmbeddingRequest],
    api_key: str,
    model: str,
    display_name: str,
    max_inline_bytes: int,
) -> str:
    """
    Submit in-memory chunks to Gemini batch embeddings and return the job name.
    """
    if not requests:
        raise ValueError("At least one chunk is required for embedding submission.")

    total_payload_bytes = 0
    for request in requests:
        content = request.get("content", {})
        for part in content.get("parts", []):
            text = part.get("text")
            if text:
                total_payload_bytes += len(text.encode("utf-8"))

    if total_payload_bytes > max_inline_bytes:
        raise ValueError(
            f"Inline embedding payload exceeds limit: {total_payload_bytes} > {max_inline_bytes} bytes."
        )

    client = genai.Client(api_key=api_key).aio
    try:
        logger.info(
            "Submitting inline embedding batch",
            extra={"chunk_count": len(requests), "model": model},
        )
        batch_job = await client.batches.create_embeddings(
            model=model,
            src=list(requests),
            config=types.CreateBatchJobConfig(display_name=display_name),
        )
        logger.info("Embedding batch created", extra={"job_name": batch_job.name})
        return batch_job.name
    except APIError:
        logger.exception("Gemini API error during inline embedding submission")
        raise
    except Exception:
        logger.exception("Unexpected failure during inline embedding submission")
        raise
    finally:
        await client.aclose()
