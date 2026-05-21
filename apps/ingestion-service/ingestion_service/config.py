"""Application config loaded from environment."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    service_name: str = "ingestion-service"

    # Valkey / broker
    valkey_url: str = "redis://localhost:6379/0"
    stream_inference: str = "inference.v1"
    stream_tool_execution: str = "tool_executions.v1"
    stream_application: str = "application.v1"
    stream_maxlen: int = 1_000_000  # approximate maxlen; XADD MAXLEN ~ N

    # Idempotency window
    idempotency_ttl_s: int = 600

    # PII
    pii_enabled: bool = True
    pii_recognizers: str = "EMAIL_ADDRESS,PHONE_NUMBER,CREDIT_CARD,US_SSN,IBAN_CODE,IP_ADDRESS,PERSON,LOCATION"
    pii_anonymize_template: str = "<{}>"

    # Auth
    sdk_api_key: str | None = None

    # Limits
    max_events_per_batch: int = 500


settings = Settings()
