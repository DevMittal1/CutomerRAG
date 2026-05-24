from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EmbeddingSyncWorkerSettings(BaseSettings):
    """
    Configuration for the Gemini embedding polling and Qdrant sync worker.
    """

    MONGODB_URI: str = Field(default="mongodb://localhost:27017")
    MONGODB_DB_NAME: str = Field(default="rag_prod")

    COLL_DOCUMENTS: str = Field(default="documents")
    COLL_CHUNKS: str = Field(default="chunks")

    GOOGLE_API_KEY: str = Field(default="")
    GEMINI_EMBEDDING_MODEL: str = Field(default="models/gemini-embedding-2")

    QDRANT_URL: str = Field(default="http://localhost:6333")
    QDRANT_API_KEY: str | None = Field(default=None)
    QDRANT_COLLECTION_NAME: str = Field(default="document_chunks")
    QDRANT_VECTOR_SIZE: int = Field(default=512)
    QDRANT_DISTANCE: str = Field(default="Cosine")
    QDRANT_DENSE_VECTOR_NAME: str | None = Field(default=None)
    QDRANT_SPARSE_VECTOR_NAME: str | None = Field(default=None)
    QDRANT_SPARSE_ON_DISK: bool = Field(default=False)
    QDRANT_UPSERT_BATCH_SIZE: int = Field(default=64)

    EMBEDDING_POLL_INTERVAL_SECONDS: float = Field(default=30.0)
    EMBEDDING_POLL_CLAIM_LIMIT: int = Field(default=50)
    EMBEDDING_POLL_CONCURRENCY: int = Field(default=10)
    EMBEDDING_LOCK_TIMEOUT_SECONDS: int = Field(default=120)
    EMBEDDING_JOB_TIMEOUT_SECONDS: int = Field(default=86400)
    EMBEDDING_BASE_RETRY_SECONDS: float = Field(default=30.0)
    EMBEDDING_BACKOFF_MULTIPLIER: float = Field(default=1.5)
    EMBEDDING_MAX_RETRY_SECONDS: float = Field(default=900.0)
    EMBEDDING_JITTER_SECONDS: float = Field(default=5.0)
    EMBEDDING_POLL_RUN_ONCE: bool = Field(default=False)

    model_config = SettingsConfigDict(
        env_file=(".env", "apps/.env", "../.env", "../apps/.env", "../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = EmbeddingSyncWorkerSettings()
