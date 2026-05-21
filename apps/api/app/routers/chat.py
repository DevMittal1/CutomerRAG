import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.auth import get_current_user
from app.db import get_db
from app.logging_config import get_logger
from app.rate_limiter import rate_limit_dependency
from app.schemas import ChatStreamRequest
from app.services.rag_chat import rag_chat_service

logger = get_logger("app.routers.chat")

router = APIRouter(prefix="/chat", tags=["Chat"])


def _sse(event: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=True)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


@router.post(
    "/stream",
    summary="Stream an AI chat response grounded by RAG retrieval",
    description=(
        "Authenticates the caller, retrieves authorized context from Qdrant via "
        "the RAG engine, and streams the grounded Gemini response over SSE."
    ),
    dependencies=[Depends(rate_limit_dependency)],
)
async def stream_chat(
    payload: ChatStreamRequest,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[Any, Depends(get_db)],
):
    user_id = str(current_user["_id"])
    logger.info(
        "Starting chat stream",
        extra={"user_id": user_id, "message_count": len(payload.messages)},
    )

    async def event_stream():
        try:
            yield _sse("start", {"status": "started"})
            async for item in rag_chat_service.stream_chat(
                payload=payload,
                current_user=current_user,
                db=db,
                request=request,
            ):
                yield _sse(item["event"], item["data"])
        except Exception as exc:
            logger.exception("Chat stream failed", extra={"user_id": user_id})
            yield _sse(
                "error",
                {
                    "message": "Chat generation failed unexpectedly.",
                    "error_type": exc.__class__.__name__,
                },
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
