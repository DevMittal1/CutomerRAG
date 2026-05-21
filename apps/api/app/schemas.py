import re
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, field_validator

# Regex for email syntax validation
EMAIL_REGEX = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"

# Allowed MIME types for RAG documents (PDF, Text, Markdown, Docx, JSON)
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "text/plain",
    "text/markdown",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/json"
}

class UserSignUp(BaseModel):
    """
    Schema for user registration requests. Enforces security requirements at the boundary.
    """
    email: str = Field(..., description="Primary email address")
    password: str = Field(..., description="Password (min 8 chars, must contain uppercase, lowercase, digit, and special symbol)")

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(EMAIL_REGEX, v):
            raise ValueError("Invalid email format.")
        return v

    @field_validator("password")
    @classmethod
    def validate_password_complexity(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long.")
        if not any(char.isupper() for char in v):
            raise ValueError("Password must contain at least one uppercase letter.")
        if not any(char.islower() for char in v):
            raise ValueError("Password must contain at least one lowercase letter.")
        if not any(char.isdigit() for char in v):
            raise ValueError("Password must contain at least one digit.")
        if not any(char in "!@#$%^&*()_+-=[]{}|;:',.<>?/~`" for char in v):
            raise ValueError("Password must contain at least one special character.")
        return v

class UserSignIn(BaseModel):
    """
    Schema for credentials validation during authentication.
    """
    email: str = Field(..., description="Primary email address")
    password: str = Field(..., description="Password")

    @field_validator("email")
    @classmethod
    def sanitize_email(cls, v: str) -> str:
        return v.strip().lower()

class UserResponse(BaseModel):
    """
    Schema for secure user response representation (never exposes password details).
    """
    id: str = Field(..., description="Stringified user ObjectId")
    email: str
    created_at: datetime

    class Config:
        from_attributes = True

class Token(BaseModel):
    """
    Schema for active session tokens issued after successful login.
    """
    access_token: str
    token_type: str = "bearer"
    expires_in_seconds: int

class TokenData(BaseModel):
    """
    Decoded token payload structure for secure session verification.
    """
    user_id: str
    email: str

class PresignedUrlRequest(BaseModel):
    """
    Schema requesting an S3 pre-signed upload URL.
    Validates document eligibility before contacting AWS.
    """
    filename: str = Field(..., min_length=1, max_length=255, description="Original name of the file")
    content_type: str = Field(..., description="Standard MIME type of the document")
    file_size_bytes: int = Field(..., gt=0, le=52428800, description="Size of the file (max 50MB)")

    @field_validator("content_type")
    @classmethod
    def validate_mime_type(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ALLOWED_MIME_TYPES:
            raise ValueError(
                f"Unsupported file type '{v}'. Allowed types are: {', '.join(ALLOWED_MIME_TYPES)}"
            )
        return v

    @field_validator("filename")
    @classmethod
    def sanitize_filename(cls, v: str) -> str:
        # Sanitize filename: remove path traversals and keep only safe alphanumeric/dot/dash characters
        v = re.sub(r"[^\w\-_.]", "_", v)
        return v

class PresignedUrlResponse(BaseModel):
    """
    Schema containing generated S3 upload details and metadata tracking.
    """
    upload_url: str = Field(..., description="Direct AWS S3 pre-signed PUT URL")
    file_key: str = Field(..., description="Unique S3 storage key mapping")
    bucket: str = Field(..., description="Target AWS S3 bucket name")
    expires_in_seconds: int
    document_id: str = Field(..., description="Tracking database ID for the upload job")

class DocumentResponse(BaseModel):
    """
    Schema for listing and tracking document metadata objects.
    """
    id: str = Field(..., description="Database tracking ID")
    filename: str
    file_key: str
    bucket: str
    status: str = Field(..., description="Lifecycle state of the document as it moves through upload and processing")
    content_type: str
    file_size_bytes: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


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
