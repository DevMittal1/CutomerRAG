from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator

class ChatMessage(BaseModel):
    role: Literal["user", "assistant"] = Field(
        ..., description="Conversation role for the message."
    )
    content: str = Field(..., min_length=1, max_length=20000)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("Message content cannot be empty.")
        return value

class ChatStreamRequest(BaseModel):
    messages: List[ChatMessage] = Field(..., min_length=1, max_length=50)
    document_ids: Optional[List[str]] = Field(
        default=None,
        description="Optional subset of owned document ids to scope retrieval.",
        max_length=1000,
    )

    @field_validator("messages")
    @classmethod
    def ensure_last_message_is_user(cls, messages: List[ChatMessage]) -> List[ChatMessage]:
        if messages[-1].role != "user":
            raise ValueError("The last message must be from the user.")
        return messages

class ChatCitation(BaseModel):
    reference_id: int
    document_id: str
    file_key: Optional[str] = None
    title: str
    source: str
    page: Optional[int] = None
    score: float
    snippet: str
