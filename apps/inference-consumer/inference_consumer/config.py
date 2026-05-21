"""Config for the inference / tool-execution consumer."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    service_name: str = "inference-consumer"

    valkey_url: str = "redis://localhost:6379/0"
    stream_inference: str = "inference.v1"
    stream_tool_execution: str = "tool_executions.v1"
    stream_inference_dlq: str = "inference.v1.dlq"
    stream_tool_execution_dlq: str = "tool_executions.v1.dlq"

    consumer_group: str = "ch-writer"
    consumer_name: str = "consumer-1"
    batch_size: int = 200
    polling_interval_ms: int = 1000

    # ClickHouse
    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_user: str = "ollive"
    clickhouse_password: str = "ollivepass"
    clickhouse_db: str = "ollive"

    inference_table: str = "inference_logs"
    tool_execution_table: str = "tool_executions"


settings = Settings()
