from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # CORS
    cors_allow_origins: str = "http://localhost:3000"

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
