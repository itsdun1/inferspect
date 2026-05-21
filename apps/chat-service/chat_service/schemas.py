"""API DTOs for the chat service (request/response Pydantic models)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MessageDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    status: str
    created_at: datetime


class ConversationDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str | None
    status: str
    model: str | None
    message_count: int
    created_at: datetime
    updated_at: datetime


class ConversationListItemDTO(ConversationDTO):
    pass


class ConversationCreateRequest(BaseModel):
    title: str | None = None
    model: str | None = None


class SendMessageRequest(BaseModel):
    conversation_id: UUID | None = None
    content: str = Field(..., min_length=1, max_length=20_000)
    model: str | None = None


class SendMessageResponse(BaseModel):
    conversation_id: UUID
    user_message: MessageDTO
    assistant_message: MessageDTO
    inference_request_id: UUID | None = None
    latency_ms: int | None = None
    total_tokens: int | None = None


class TranscriptResponse(BaseModel):
    conversation: ConversationDTO
    messages: list[MessageDTO]
