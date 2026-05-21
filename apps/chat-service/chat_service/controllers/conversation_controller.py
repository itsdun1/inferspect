"""Conversations CRUD."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status

from chat_service.deps import (
    ChatServiceDep,
    ConversationServiceDep,
    CurrentUserDep,
    SessionDep,
)
from chat_service.schemas import (
    ConversationCreateRequest,
    ConversationDTO,
    ConversationListItemDTO,
    TranscriptResponse,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("", response_model=list[ConversationListItemDTO])
async def list_conversations(
    session: SessionDep,
    user: CurrentUserDep,
    svc: ConversationServiceDep,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list:
    return await svc.list_for_user(session=session, user=user, limit=limit, offset=offset)


@router.post("", response_model=ConversationDTO, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: ConversationCreateRequest,
    session: SessionDep,
    user: CurrentUserDep,
    svc: ConversationServiceDep,
) -> ConversationDTO:
    convo = await svc.create(session=session, user=user, title=body.title, model=body.model)
    return ConversationDTO.model_validate(convo)


@router.get("/{conversation_id}", response_model=TranscriptResponse)
async def get_transcript(
    conversation_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
    svc: ConversationServiceDep,
) -> TranscriptResponse:
    convo, msgs = await svc.transcript(
        session=session, user=user, conversation_id=conversation_id
    )
    return TranscriptResponse(
        conversation=ConversationDTO.model_validate(convo),
        messages=[__import__("chat_service.schemas", fromlist=["MessageDTO"]).MessageDTO.model_validate(m) for m in msgs],
    )


@router.post("/{conversation_id}/cancel", response_model=ConversationDTO)
async def cancel_conversation(
    conversation_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
    svc: ConversationServiceDep,
    chat_svc: ChatServiceDep,
) -> ConversationDTO:
    """Cancel any in-flight stream for this conversation AND mark the
    conversation as cancelled in the DB."""
    await chat_svc.registry.cancel(conversation_id)
    convo = await svc.cancel(session=session, user=user, conversation_id=conversation_id)
    return ConversationDTO.model_validate(convo)
