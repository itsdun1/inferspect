"""Conversation repository."""

from __future__ import annotations

import uuid

from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from chat_service.db.models import Conversation


async def get_by_id(session: AsyncSession, conversation_id: uuid.UUID) -> Conversation | None:
    return (
        await session.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()


async def get_for_user(
    session: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> Conversation | None:
    return (
        await session.execute(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.user_id == user_id
            )
        )
    ).scalar_one_or_none()


async def list_for_user(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int = 20, offset: int = 0
) -> list[Conversation]:
    res = await session.execute(
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .order_by(desc(Conversation.updated_at))
        .limit(limit)
        .offset(offset)
    )
    return list(res.scalars().all())


async def create(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    title: str | None = None,
    model: str | None = None,
) -> Conversation:
    convo = Conversation(user_id=user_id, title=title, model=model, status="active")
    session.add(convo)
    await session.flush()
    return convo


async def set_status(
    session: AsyncSession, conversation_id: uuid.UUID, *, status: str
) -> None:
    await session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(status=status, updated_at=func.now())
    )


async def bump_for_message(
    session: AsyncSession, conversation_id: uuid.UUID, *, model: str | None
) -> None:
    """Bump ``updated_at`` and ``message_count``; refresh denormalized model."""
    values = {
        "message_count": Conversation.message_count + 1,
        "updated_at": func.now(),
    }
    if model is not None:
        values["model"] = model
    await session.execute(
        update(Conversation).where(Conversation.id == conversation_id).values(**values)
    )
