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

from contracts.ingestion import (
    ALLOWED_MIME_TYPES,
    PresignedUrlRequest,
    PresignedUrlResponse,
    DocumentResponse,
)
from contracts.retrieval import (
    ChatMessage,
    ChatStreamRequest,
    ChatCitation,
)

