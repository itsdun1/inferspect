"""UserManager — fastapi-users hook surface."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import Request
from fastapi_users import BaseUserManager, UUIDIDMixin
from fastapi_users.db import SQLAlchemyUserDatabase

from chat_service.config import settings
from chat_service.db.models import User

log = logging.getLogger(__name__)


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    reset_password_token_secret = settings.jwt_secret
    verification_token_secret = settings.jwt_secret

    async def on_after_register(self, user: User, request: Request | None = None) -> None:
        log.info("user registered: %s (id=%s)", user.email, user.id)

    async def on_after_login(
        self,
        user: User,
        request: Request | None = None,
        response: Any = None,
    ) -> None:
        log.debug("user logged in: %s", user.email)


async def get_user_manager(user_db: SQLAlchemyUserDatabase) -> UserManager:
    yield UserManager(user_db)
