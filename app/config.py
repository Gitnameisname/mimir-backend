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
    jwt_expire_minutes: int = 15  # Phase 14: RT 도입으로 120 → 15분 단축
    jwt_refresh_expire_days: int = 7
    internal_service_secret: str = ""

    # Password policy (Phase 14)
    bcrypt_cost_factor: int = 12
    login_max_attempts: int = 5
    login_lockout_minutes: int = 15

    # GitLab OAuth (Phase 14-4)
    gitlab_base_url: str = "https://gitlab.com"
    gitlab_client_id: str = ""
    gitlab_client_secret: str = ""
    gitlab_redirect_uri: str = ""
    oauth_token_encryption_key: str = ""  # 32바이트 base64 인코딩
    frontend_url: str = "http://localhost:3000"  # OAuth 콜백 후 프론트엔드 리다이렉트

    # SMTP (Phase 14-5)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_address: str = "noreply@mimir.local"
    smtp_use_tls: bool = False

    # CORS
    cors_allow_origins: str = "http://localhost:3000"

    # OpenAI / Embedding
    openai_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_batch_size: int = 100

    # LLM (Phase 11 RAG)
    llm_provider: str = "openai"           # "openai" | "anthropic"
    # OpenAI: gpt-4o-mini (모든 프로젝트 기본 허용) / gpt-4o (별도 권한 필요)
    # Anthropic: claude-sonnet-4-6
    llm_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    rag_top_k: int = 20                    # Retriever Top-K
    rag_top_n: int = 5                     # Reranker Top-N (최종 컨텍스트 청크 수)
    rag_reranker_enabled: bool = True
    rag_reranker_threshold: float = 0.0    # 최소 유사도 점수 (0.0 = 비활성)
    rag_max_context_tokens: int = 6000     # ContextBuilder 최대 토큰 수
    rag_max_history_turns: int = 10        # Multi-turn 최대 이전 대화 수

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
    def is_oauth_enabled(self) -> bool:
        """GitLab OAuth가 설정되어 있는지 확인."""
        return bool(self.gitlab_client_id and self.gitlab_client_secret)

    @property
    def oauth_encryption_key_bytes(self) -> bytes | None:
        """OAUTH_TOKEN_ENCRYPTION_KEY를 bytes로 디코딩. 미설정 시 None."""
        if not self.oauth_token_encryption_key:
            return None
        import base64
        return base64.b64decode(self.oauth_token_encryption_key)

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
