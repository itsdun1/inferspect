"""Pydantic schemas for the three log types the SDK emits."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


SCHEMA_VERSION = "1.0"


class LogType(StrEnum):
    INFERENCE = "inference"
    TOOL_EXECUTION = "tool_execution"
    APPLICATION = "application"


class Status(StrEnum):
    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class FinishReason(StrEnum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    CANCELLED = "cancelled"


class ToolCallSummary(BaseModel):
    """A single tool call announced by the model (not yet executed)."""

    name: str
    args_preview: str = ""


class _BaseLog(BaseModel):
    """Shared fields across all log types."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    schema_version: str = SCHEMA_VERSION
    log_type: LogType
    service: str
    sdk_version: str | None = None


class InferenceLog(_BaseLog):
    log_type: Literal[LogType.INFERENCE] = LogType.INFERENCE

    # Server-set tenant tag. SDK leaves this as "" — ingestion resolves the
    # API key into a client_name and stamps the event before publishing.
    client: str = ""

    request_id: UUID
    conversation_id: UUID | None = None
    session_id: UUID | None = None
    user_id: UUID | None = None

    provider: str
    model: str
    stream: bool = False

    started_at: datetime
    finished_at: datetime
    latency_ms: int
    ttft_ms: int | None = None

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0

    status: Status
    finish_reason: FinishReason | None = None
    tool_calls_count: int = 0
    tool_calls_summary: list[ToolCallSummary] = Field(default_factory=list)

    error_code: str | None = None
    error_message: str | None = None

    input_preview: str = ""
    output_preview: str = ""

    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionLog(_BaseLog):
    log_type: Literal[LogType.TOOL_EXECUTION] = LogType.TOOL_EXECUTION

    # Server-set tenant tag. SDK leaves this as "" — ingestion resolves the
    # API key into a client_name and stamps the event before publishing.
    client: str = ""

    request_id: UUID
    tool_call_id: UUID
    parent_inference_request_id: UUID | None = None
    conversation_id: UUID | None = None
    session_id: UUID | None = None
    user_id: UUID | None = None

    tool_name: str

    started_at: datetime
    finished_at: datetime
    latency_ms: int

    status: Status
    error_code: str | None = None
    error_message: str | None = None

    args_preview: str = ""
    result_preview: str = ""
    result_size_bytes: int = 0

    metadata: dict[str, Any] = Field(default_factory=dict)


class ApplicationLog(_BaseLog):
    log_type: Literal[LogType.APPLICATION] = LogType.APPLICATION

    # Server-set tenant tag. SDK leaves this as "" — ingestion resolves the
    # API key into a client_name and stamps the event before publishing.
    client: str = ""

    ts: datetime
    level: str
    logger: str = ""
    trace_id: UUID | None = None
    span_id: str | None = None
    message: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class LogEnvelope(BaseModel):
    """Batch envelope the SDK POSTs to the ingestion service."""

    model_config = ConfigDict(extra="forbid")

    service: str
    sdk_version: str
    events: list[InferenceLog | ToolExecutionLog | ApplicationLog]
