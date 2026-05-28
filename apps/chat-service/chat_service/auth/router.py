"""Wires fastapi-users routers into a single ``auth_router`` to mount under /auth."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter
from fastapi_users import schemas
from pydantic import ConfigDict

from chat_service.auth.backends import auth_backend
from chat_service.auth.deps import fastapi_users


class UserRead(schemas.BaseUser[uuid.UUID]):
    model_config = ConfigDict(from_attributes=True)

    # Override fastapi-users' EmailStr — pydantic v2's validator rejects
    # reserved TLDs like ``.local`` which we use for local dev/demo accounts.
    # Existing accounts already in the DB would otherwise fail to deserialize.
    email: str  # type: ignore[assignment]

    role: str
    created_at: datetime


class UserCreate(schemas.BaseUserCreate):
    # Same override on create so registration accepts ``.local``.
    email: str  # type: ignore[assignment]


class UserUpdate(schemas.BaseUserUpdate):
    email: str | None = None  # type: ignore[assignment]


auth_router = APIRouter(prefix="/auth", tags=["auth"])

# /auth/login + /auth/logout
auth_router.include_router(fastapi_users.get_auth_router(auth_backend))
# /auth/register
auth_router.include_router(
    fastapi_users.get_register_router(UserRead, UserCreate)
)
# /users/me (under /auth/users so we can mount the prefix together)
auth_router.include_router(
    fastapi_users.get_users_router(UserRead, UserUpdate),
    prefix="/users",
)
