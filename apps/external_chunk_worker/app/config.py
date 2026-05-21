from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class PollerSettings(BaseSettings):
    """
    Independent configuration for the Landing AI Poller Worker.
    """
    # MongoDB Connection
    MONGODB_URI: str = Field(default="mongodb://localhost:27017")
    MONGODB_DB_NAME: str = Field(default="rag_prod")

    # Collection Names
    COLL_DOCUMENTS: str = Field(default="documents")
    COLL_CHUNKS: str = Field(default="chunks")

    # Landing AI Configuration
    LANDING_AI_API_KEY: str = Field(default="")
    LANDING_AI_BASE_URL: str = Field(default="https://api.va.landing.ai/v1/ade")
    GOOGLE_API_KEY: str = Field(default="")
    GEMINI_EMBEDDING_MODEL: str = Field(default="models/gemini-embedding-2")
    GEMINI_EMBEDDING_OUTPUT_DIMENSIONALITY: int = Field(default=512)
    GEMINI_EMBEDDING_DISPLAY_NAME: str = Field(
        default="external_chunk_worker_inline_embedding_run"
    )
    GEMINI_EMBEDDING_MAX_INLINE_BYTES: int = Field(default=20 * 1024 * 1024)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = PollerSettings()
