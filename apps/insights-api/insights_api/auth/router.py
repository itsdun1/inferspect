"""Wires fastapi-users routers into a single ``operator_router`` mounted under /auth.

We deliberately omit the *register* router — operators are not self-service.
The only way to create an operator in this phase is the env-bootstrap path
(`CONSOLE_BOOTSTRAP_EMAIL` / `CONSOLE_BOOTSTRAP_PASSWORD`).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter
from fastapi_users import schemas
from pydantic import ConfigDict

from insights_api.auth.backends import auth_backend
from insights_api.auth.deps import fastapi_users


class OperatorRead(schemas.BaseUser[uuid.UUID]):
    model_config = ConfigDict(from_attributes=True)

    # Override fastapi-users' EmailStr → plain str. Reserved-TLD emails like
    # ``operator@ollive.local`` (common in dev / bootstrap) trip email-validator
    # on the read path; we still validate on write (OperatorCreate inherits the
    # strict shape).
    email: str  # type: ignore[assignment]
    created_at: datetime


class OperatorCreate(schemas.BaseUserCreate):
    pass


class OperatorUpdate(schemas.BaseUserUpdate):
    pass


operator_router = APIRouter(tags=["auth"])

# /auth/login + /auth/logout
operator_router.include_router(fastapi_users.get_auth_router(auth_backend))
# /auth/users/me, /auth/users/{id}
operator_router.include_router(
    fastapi_users.get_users_router(OperatorRead, OperatorUpdate),
    prefix="/users",
)
