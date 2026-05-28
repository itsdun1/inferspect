"""Application config loaded from environment."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    service_name: str = "insights-api"

    # ClickHouse — same env var names as inference-consumer.
    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_user: str = "ollive"
    clickhouse_password: str = "ollivepass"
    clickhouse_db: str = "ollive"

    inference_table: str = "inference_logs"
    tool_execution_table: str = "tool_executions"
    application_table: str = "application_logs"
    mv_inference_5m: str = "mv_inference_5m"
    mv_tool_5m: str = "mv_tool_5m"

    # Postgres (shared host with chat-service; we own the `operators` table only).
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "ollive"
    postgres_password: str = "ollivepass"
    postgres_db: str = "ollive"

    # Console auth — completely independent of chat-service's user auth.
    # Different secret, different cookie, different table.
    console_jwt_secret: str = "change-me-32-byte-random-hex"
    console_jwt_lifetime_seconds: int = 60 * 60 * 24 * 7  # 7 days
    cookie_secure: bool = False  # True in prod (HTTPS only)
    console_bootstrap_email: str | None = None
    console_bootstrap_password: str | None = None

    # SDK / ingestion — insights-api owns the synthetic-log generator now,
    # so it needs its own SDK logger pointed at the ingestion service.
    ingestion_url: str = "http://ingestion-service:8001/v1/logs"
    sdk_api_key: str | None = None

    # Base URL of the ingestion service for control-plane calls (kill).
    # Falls back to a sane derivation of ``ingestion_url`` when unset.
    ingestion_control_base_url: str | None = None

    @property
    def ingestion_control_url(self) -> str:
        if self.ingestion_control_base_url:
            return self.ingestion_control_base_url.rstrip("/") + "/v1/control"
        # ``ingestion_url`` looks like http://host/v1/logs — strip the /logs.
        base = self.ingestion_url.rstrip("/")
        if base.endswith("/v1/logs"):
            base = base[: -len("/logs")]
        elif base.endswith("/logs"):
            base = base[: -len("/logs")]
        else:
            base = base + "/v1"
        return base.rstrip("/") + "/control"

    # Comma-separated list of extra CORS origins (the dev ports are already allowed).
    allowed_origins: str = ""

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
