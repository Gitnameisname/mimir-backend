import logging

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_REQUIRED_IN_PRODUCTION = ["jwt_secret", "postgres_password", "internal_service_secret"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Service
    service_name: str = "mimir"
    api_version: str = "v1"
    environment: str = "development"
    debug: bool = False

    # Database
    postgres_user: str = "master"
    postgres_password: str = ""
    postgres_db: str = "mimir"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # Security
    jwt_secret: str = ""
    jwt_expire_minutes: int = 120
    internal_service_secret: str = ""
    # CORS
    cors_allow_origins: str = "http://localhost:3000"

    # OpenAI / Embedding
    openai_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_batch_size: int = 100

    # Valkey / Redis
    valkey_host: str = "localhost"
    valkey_port: int = 6379
    valkey_password: str = ""
    valkey_user: str = ""
    valkey_db: int = 0

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> "Settings":
        """VULN-008: production 환경에서 필수 시크릿 미설정 시 startup 실패."""
        if self.environment == "production":
            missing = [
                field for field in _REQUIRED_IN_PRODUCTION
                if not getattr(self, field, None)
            ]
            if missing:
                raise ValueError(
                    f"Production requires these secrets to be set: {', '.join(missing)}"
                )
        elif self.environment != "production":
            missing = [
                field for field in _REQUIRED_IN_PRODUCTION
                if not getattr(self, field, None)
            ]
            if missing:
                logger.warning(
                    "Security secrets not configured (OK for dev, required for prod): %s",
                    ", ".join(missing),
                )
        return self

    @property
    def valkey_host_clean(self) -> str:
        """VALKEY_HOST에서 scheme(http://, https://) 제거 — Redis 프로토콜에는 hostname만 필요."""
        host = self.valkey_host
        for scheme in ("https://", "http://"):
            if host.startswith(scheme):
                host = host[len(scheme):]
        return host.rstrip("/")

    @property
    def valkey_url(self) -> str:
        """redis-py / limits 라이브러리용 연결 URL."""
        user = self.valkey_user
        password = self.valkey_password
        host = self.valkey_host_clean
        port = self.valkey_port
        db = self.valkey_db
        if user and password:
            return f"redis://{user}:{password}@{host}:{port}/{db}"
        if password:
            return f"redis://:{password}@{host}:{port}/{db}"
        return f"redis://{host}:{port}/{db}"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]


settings = Settings()
