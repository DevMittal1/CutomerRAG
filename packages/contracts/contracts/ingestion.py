import re
from datetime import datetime
from pydantic import BaseModel, Field, field_validator

# Allowed MIME types for RAG documents (PDF, Text, Markdown, Docx, JSON)
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "text/plain",
    "text/markdown",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/json"
}

class PresignedUrlRequest(BaseModel):
    """
    Schema requesting an S3 pre-signed upload URL.
    Validates document eligibility before contacting AWS.
    """
    filename: str = Field(..., min_length=1, max_length=255, description="Original name of the file")
    content_type: str = Field(..., description="Standard MIME type of the document")
    file_size_bytes: int = Field(..., gt=0, le=52428800, description="Size of the file (max 50MB)")
    use_external_parser: bool = Field(default=False, description="Flag indicating if the document should be routed to an external layout/vision-based parser")

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
    use_external_parser: bool = Field(default=False, description="Whether the document was processed by an external layout/vision-based parser")

    class Config:
        from_attributes = True
