"""User repository — module-level functions so callers can ``from ... import``
without juggling a class instance. All functions take ``session`` first.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chat_service.db.models import User


async def get_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    return (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()


async def get_or_create_by_email(session: AsyncSession, *, email: str, role: str = "user") -> User:
    existing = await get_by_email(session, email)
    if existing is not None:
        return existing
    user = User(email=email, role=role)
    session.add(user)
    await session.flush()
    return user
