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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = PollerSettings()
