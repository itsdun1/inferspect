"""Chat service config — env-driven via pydantic-settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    service_name: str = "chat-service"

    # Postgres
    postgres_user: str = "ollive"
    postgres_password: str = "ollivepass"
    postgres_db: str = "ollive"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # LLM keys (only one required for the demo to work)
    gemini_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    default_provider: str = "google"
    default_model: str = "gemini-2.5-pro"

    # SDK / ingestion
    ingestion_url: str = "http://ingestion-service:8001/v1/logs"
    sdk_api_key: str | None = None

    # Memory
    memory_window: int = 12  # last N turns of context

    # Auth (fastapi-users + JWT cookie)
    jwt_secret: str = "change-me-to-a-32-byte-random-hex"
    jwt_lifetime_seconds: int = 60 * 60 * 24 * 7  # 7 days
    cookie_secure: bool = False  # set True in prod (HTTPS only)
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
