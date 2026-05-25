from typing import Annotated, Any
from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

def _parse_chunk_sizes(value):
    if isinstance(value, list):
        return [int(item) for item in value]
    if isinstance(value, str):
        return [int(item.strip()) for item in value.split(",") if item.strip()]
    return value

class ChunkWorkerSettings(BaseSettings):
    """
    Unified configuration for the local and external chunk workers.
    """
    MONGODB_URI: str = Field(default="mongodb://localhost:27017")
    MONGODB_DB_NAME: str = Field(default="rag_prod")

    # Redis stream (Local path)
    REDIS_URI: str = Field(default="redis://localhost:6379")
    REDIS_CHUNK_STREAM: str = Field(default="rag:chunks")
    REDIS_CHUNK_CONSUMER_GROUP: str = Field(default="document_chunking")
    REDIS_CHUNK_BLOCK_MS: int = Field(default=30000)
    REDIS_CHUNK_RECLAIM_IDLE_MS: int = Field(default=120000)
    REDIS_CHUNK_RECLAIM_BATCH_SIZE: int = Field(default=10)
    REDIS_CHUNK_RECLAIM_INTERVAL_SECONDS: int = Field(default=30)

    # AWS configuration (Local path)
    S3_BUCKET_NAME: str = Field(default="rag-prod-documents-bucket")
    AWS_ACCESS_KEY_ID: str = Field(default="mock_access_key_id")
    AWS_SECRET_ACCESS_KEY: str = Field(default="mock_secret_access_key")
    AWS_REGION: str = Field(default="us-east-1")

    # LlamaIndex chunking configuration (Local path)
    CHUNK_SIZES: Annotated[Any, BeforeValidator(_parse_chunk_sizes)] = Field(
        default_factory=lambda: [756, 324]
    )
    MAX_CONCURRENT_TASKS: int = Field(default=5)

    # Collections
    COLL_DOCUMENTS: str = Field(default="documents")
    COLL_CHUNKS: str = Field(default="chunks")

    # Landing AI Configuration (External path)
    LANDING_AI_API_KEY: str = Field(default="")
    LANDING_AI_BASE_URL: str = Field(default="https://api.va.landing.ai/v1/ade")
    LANDING_AI_JOB_TIMEOUT_SECONDS: int = Field(default=3600)

    # Gemini embedding configuration
    GOOGLE_API_KEY: str = Field(default="")
    GEMINI_EMBEDDING_MODEL: str = Field(default="models/gemini-embedding-2")
    GEMINI_EMBEDDING_OUTPUT_DIMENSIONALITY: int = Field(default=512)
    GEMINI_EMBEDDING_DISPLAY_NAME: str = Field(
        default="chunk_worker_inline_embedding_run"
    )
    GEMINI_EMBEDDING_MAX_INLINE_BYTES: int = Field(default=20 * 1024 * 1024)

    # Worker Mode (local | external | both)
    CHUNK_WORKER_MODE: str = Field(default="both")

    model_config = SettingsConfigDict(
        env_file=(".env", "apps/.env", "../.env", "../apps/.env", "../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

settings = ChunkWorkerSettings()
