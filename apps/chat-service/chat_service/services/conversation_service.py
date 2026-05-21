"""Conversation orchestration — list, fetch, create, cancel."""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from chat_service.db.models import Conversation, Message, User
from chat_service.repositories import conversation_repository, message_repository


class ConversationService:
    """Stateless — instance keeps no per-request state."""

    async def list_for_user(
        self, *, session: AsyncSession, user: User, limit: int = 20, offset: int = 0
    ) -> list[Conversation]:
        return await conversation_repository.list_for_user(
            session, user.id, limit=limit, offset=offset
        )

    async def get_for_user(
        self, *, session: AsyncSession, user: User, conversation_id: uuid.UUID
    ) -> Conversation:
        convo = await conversation_repository.get_for_user(session, conversation_id, user.id)
        if convo is None and user.role != "admin":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found")
        if convo is None:
            convo = await conversation_repository.get_by_id(session, conversation_id)
            if convo is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found"
                )
        return convo

    async def create(
        self,
        *,
        session: AsyncSession,
        user: User,
        title: str | None = None,
        model: str | None = None,
    ) -> Conversation:
        return await conversation_repository.create(
            session, user_id=user.id, title=title, model=model
        )

    async def transcript(
        self, *, session: AsyncSession, user: User, conversation_id: uuid.UUID
    ) -> tuple[Conversation, list[Message]]:
        convo = await self.get_for_user(
            session=session, user=user, conversation_id=conversation_id
        )
        msgs = await message_repository.list_for_conversation(session, convo.id)
        return convo, msgs

    async def cancel(
        self, *, session: AsyncSession, user: User, conversation_id: uuid.UUID
    ) -> Conversation:
        convo = await self.get_for_user(
            session=session, user=user, conversation_id=conversation_id
        )
        await conversation_repository.set_status(session, convo.id, status="cancelled")
        await session.refresh(convo)
        return convo
