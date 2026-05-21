"""Message repository."""

from __future__ import annotations

import uuid

from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncSession

from chat_service.db.models import Message


async def list_for_conversation(
    session: AsyncSession, conversation_id: uuid.UUID, *, limit: int | None = None
) -> list[Message]:
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(asc(Message.created_at))
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def list_tail(
    session: AsyncSession, conversation_id: uuid.UUID, *, n: int
) -> list[Message]:
    """Return the most recent ``n`` messages in chronological order."""
    res = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(n)
    )
    msgs = list(res.scalars().all())
    msgs.reverse()
    return msgs


async def create(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    role: str,
    content: str,
    content_redacted: str | None = None,
    inference_request_id: uuid.UUID | None = None,
    status: str = "complete",
) -> Message:
    msg = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        content_redacted=content_redacted,
        inference_request_id=inference_request_id,
        status=status,
    )
    session.add(msg)
    await session.flush()
    return msg
