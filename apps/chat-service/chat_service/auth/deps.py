"""FastAPI dependencies built on fastapi-users."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, status
from fastapi_users import FastAPIUsers
from fastapi_users.db import SQLAlchemyUserDatabase
from sqlalchemy.ext.asyncio import AsyncSession

from chat_service.auth.backends import auth_backend
from chat_service.auth.manager import UserManager, get_user_manager
from chat_service.db.models import User
from chat_service.db.session import get_session


async def get_user_db(session: AsyncSession = Depends(get_session)) -> AsyncIterator[SQLAlchemyUserDatabase]:
    yield SQLAlchemyUserDatabase(session, User)


async def get_user_manager_dep(
    user_db: SQLAlchemyUserDatabase = Depends(get_user_db),
) -> AsyncIterator[UserManager]:
    yield UserManager(user_db)


fastapi_users: FastAPIUsers[User, uuid.UUID] = FastAPIUsers[User, uuid.UUID](
    get_user_manager_dep,
    [auth_backend],
)

current_active_user = fastapi_users.current_user(active=True)


async def current_admin_user(user: User = Depends(current_active_user)) -> User:
    """Require an active user with admin privileges. We treat ``role='admin'``
    as the source of truth; ``is_superuser`` mirrors it for fastapi-users
    semantics."""
    if user.role != "admin" and not user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin only",
        )
    return user
