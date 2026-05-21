"""Config."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    service_name: str = "app-log-consumer"

    valkey_url: str = "redis://localhost:6379/0"
    stream_application: str = "application.v1"
    stream_application_dlq: str = "application.v1.dlq"

    consumer_group: str = "ch-writer"
    consumer_name: str = "app-log-consumer-1"
    batch_size: int = 200
    polling_interval_ms: int = 1000

    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_user: str = "ollive"
    clickhouse_password: str = "ollivepass"
    clickhouse_db: str = "ollive"

    application_table: str = "application_logs"


settings = Settings()
