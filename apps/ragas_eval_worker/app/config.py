from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RagasEvalWorkerSettings(BaseSettings):
    MONGODB_URI: str = Field(default="mongodb://localhost:27017")
    MONGODB_DB_NAME: str = Field(default="rag_prod")

    GOOGLE_API_KEY: str = Field(default="")
    GEMINI_API_KEY: str = Field(default="")
    RAG_EVAL_TRACE_COLLECTION: str = Field(default="rag_evaluation_traces")
    RAGAS_EVAL_MODEL: str = Field(default="gemini-2.0-flash")
    RAGAS_EVAL_EMBEDDING_MODEL: str = Field(default="text-embedding-004")
    RAGAS_EVAL_POLL_INTERVAL_SECONDS: float = Field(default=15.0)
    RAGAS_EVAL_BATCH_SIZE: int = Field(default=4)
    RAGAS_EVAL_LOCK_TIMEOUT_SECONDS: int = Field(default=300)
    RAGAS_EVAL_MAX_ATTEMPTS: int = Field(default=5)
    RAGAS_EVAL_RUN_ONCE: bool = Field(default=False)

    model_config = SettingsConfigDict(
        env_file=(".env", "apps/.env", "../.env", "../apps/.env", "../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def effective_google_api_key(self) -> str:
        return self.GEMINI_API_KEY or self.GOOGLE_API_KEY


settings = RagasEvalWorkerSettings()
