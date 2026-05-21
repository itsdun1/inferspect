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

    # Comma-separated list of extra CORS origins (the dev ports are already allowed).
    allowed_origins: str = ""


settings = Settings()
