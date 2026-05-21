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

    # Redis Connection
    REDIS_URI: str = Field(default="redis://localhost:6379")
    REDIS_CHUNK_STREAM: str = Field(default="rag:chunks")
    REDIS_CHUNK_CONSUMER_GROUP: str = Field(default="document_chunking")
    REDIS_CHUNK_BLOCK_MS: int = Field(default=30000)
    REDIS_CHUNK_RECLAIM_IDLE_MS: int = Field(default=120000)
    REDIS_CHUNK_RECLAIM_BATCH_SIZE: int = Field(default=10)
    REDIS_CHUNK_RECLAIM_INTERVAL_SECONDS: int = Field(default=30)

    # AWS Configuration
    AWS_ACCESS_KEY_ID: str = Field(default="mock_access_key_id")
    AWS_SECRET_ACCESS_KEY: str = Field(default="mock_secret_access_key")
    AWS_REGION: str = Field(default="us-east-1")
    S3_BUCKET_NAME: str = Field(default="rag-prod-documents-bucket")
    
    # SQS Configuration
    AWS_SQS_QUEUE_URL: str = Field(default="https://sqs.us-east-1.amazonaws.com/123456789012/rag-ingestion-queue")
    AWS_SQS_WAIT_TIME_SECONDS: int = Field(default=20)
    AWS_SQS_VISIBILITY_TIMEOUT: int = Field(default=30)

    # RAG Tuning
    CHUNK_SIZE: int = Field(default=512)
    CHUNK_OVERLAP: int = Field(default=50)

    # Worker Scaling
    MAX_CONCURRENT_TASKS: int = Field(default=5)

    # Collection Names
    COLL_DOCUMENTS: str = Field(default="documents")
    COLL_CHUNKS: str = Field(default="chunks")

    # Landing AI Configuration
    LANDING_AI_API_KEY: str = Field(default="")
    LANDING_AI_BASE_URL: str = Field(default="https://api.va.landing.ai/v1/ade")

    model_config = SettingsConfigDict(
        env_file=".env",  # Traverse up to the root .env
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = WorkerSettings()
