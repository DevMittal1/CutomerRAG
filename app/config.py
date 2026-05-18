import json
from typing import List, Union
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Server Binding Settings (Avoids hardcoded 0.0.0.0 to satisfy security linters)
    HOST: str = Field(default="127.0.0.1")
    PORT: int = Field(default=8000)

    # MongoDB Settings
    MONGODB_URI: str = Field(default="mongodb://localhost:27017")
    MONGODB_DB_NAME: str = Field(default="rag_prod")

    # JWT Authentication
    JWT_SECRET_KEY: str = Field(default="949f57912bbbc4f7831f24d45a98bf24c5b96677f98d4076f7bb8f1d82121de4")
    JWT_ALGORITHM: str = Field(default="HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=60)

    # AWS S3 Configuration
    AWS_ACCESS_KEY_ID: str = Field(default="mock_access_key_id")
    AWS_SECRET_ACCESS_KEY: str = Field(default="mock_secret_access_key")
    AWS_REGION: str = Field(default="us-east-1")
    S3_BUCKET_NAME: str = Field(default="rag-prod-documents-bucket")

    # CORS Settings
    CORS_ORIGINS: Union[str, List[str]] = Field(default=["http://localhost:3000", "http://localhost:8000"])

    # Rate Limiting
    RATE_LIMIT_REQUESTS: int = Field(default=100)
    RATE_LIMIT_WINDOW_SECONDS: int = Field(default=60)
    RATE_LIMIT_BYPASS_IPS: Union[str, List[str]] = Field(default=["127.0.0.1", "::1"])

    # App Environment
    ENV: str = Field(default="production")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    def get_cors_origins(self) -> List[str]:
        """Safely parse CORS_ORIGINS from env into a list of strings."""
        if isinstance(self.CORS_ORIGINS, str):
            try:
                parsed = json.loads(self.CORS_ORIGINS)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
            return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]
        return self.CORS_ORIGINS

    def get_bypass_ips(self) -> List[str]:
        """Safely parse RATE_LIMIT_BYPASS_IPS from env into a list of strings."""
        if isinstance(self.RATE_LIMIT_BYPASS_IPS, str):
            try:
                parsed = json.loads(self.RATE_LIMIT_BYPASS_IPS)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
            return [ip.strip() for ip in self.RATE_LIMIT_BYPASS_IPS.split(",") if ip.strip()]
        return self.RATE_LIMIT_BYPASS_IPS

settings = Settings()
