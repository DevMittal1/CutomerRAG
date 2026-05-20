from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class WorkerSettings(BaseSettings):
    """
    Independent configuration for the RAG Ingestion Worker.
    Kept separate from the API settings for better modularity.
    """
    # MongoDB Connection (Shared logic, but independent instance)
    MONGODB_URI: str = Field(default="mongodb://localhost:27017")
    MONGODB_DB_NAME: str = Field(default="rag_prod")

    # RAG Tuning
    CHUNK_SIZE: int = Field(default=512)
    CHUNK_OVERLAP: int = Field(default=50)

    # Worker Scaling
    MAX_CONCURRENT_TASKS: int = Field(default=5)

    # Landing AI Configuration
    LANDING_AI_API_KEY: Optional[str] = Field(default=None)
    LANDING_AI_BASE_URL: str = Field(default="https://api.va.landing.ai/v1/ade")
    
    # MongoDB Collections
    COLL_DOCUMENTS: str = Field(default="documents")
    COLL_CHUNKS: str = Field(default="document_chunks")

    model_config = SettingsConfigDict(
        env_file=".env",  # Traverse up to the root .env
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = WorkerSettings()
