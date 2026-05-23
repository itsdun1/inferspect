"""OperatorManager — fastapi-users hook surface for the operator console."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import Request
from fastapi_users import BaseUserManager, UUIDIDMixin
from fastapi_users.db import SQLAlchemyUserDatabase

from insights_api.config import settings
from insights_api.db.models import Operator

log = logging.getLogger(__name__)


class OperatorManager(UUIDIDMixin, BaseUserManager[Operator, uuid.UUID]):
    reset_password_token_secret = settings.console_jwt_secret
    verification_token_secret = settings.console_jwt_secret

    async def on_after_register(self, user: Operator, request: Request | None = None) -> None:
        log.info("operator registered: %s (id=%s)", user.email, user.id)

    async def on_after_login(
        self,
        user: Operator,
        request: Request | None = None,
        response: Any = None,
    ) -> None:
        log.debug("operator logged in: %s", user.email)


async def get_operator_manager(user_db: SQLAlchemyUserDatabase) -> OperatorManager:
    yield OperatorManager(user_db)
