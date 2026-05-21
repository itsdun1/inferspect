"""API DTOs.

The wire schemas for log events live in ``chatbot_sdk.schema`` so SDK and
ingestion share the contract. This module only adds the request/response
shapes specific to the HTTP endpoint.
"""

from __future__ import annotations

from typing import Literal

from chatbot_sdk.schema import LogEnvelope  # re-export for controller use
from pydantic import BaseModel, Field


__all__ = ["LogEnvelope", "EventStatus", "IngestResponse"]


class EventStatus(BaseModel):
    request_id: str | None = None
    status: Literal["accepted", "duplicate", "rejected"]
    reason: str | None = None


class IngestResponse(BaseModel):
    accepted: int
    duplicates: int = 0
    rejected: int = 0
    events: list[EventStatus] = Field(default_factory=list)
