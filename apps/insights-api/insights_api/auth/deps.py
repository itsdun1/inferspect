"""FastAPI dependencies built on fastapi-users for the operator console."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import Depends
from fastapi_users import FastAPIUsers
from fastapi_users.db import SQLAlchemyUserDatabase
from sqlalchemy.ext.asyncio import AsyncSession

from insights_api.auth.backends import auth_backend
from insights_api.auth.manager import OperatorManager, get_operator_manager
from insights_api.db.models import Operator
from insights_api.db.session import get_session


async def get_operator_db(
    session: AsyncSession = Depends(get_session),
) -> AsyncIterator[SQLAlchemyUserDatabase]:
    yield SQLAlchemyUserDatabase(session, Operator)


async def get_operator_manager_dep(
    user_db: SQLAlchemyUserDatabase = Depends(get_operator_db),
) -> AsyncIterator[OperatorManager]:
    yield OperatorManager(user_db)


fastapi_users: FastAPIUsers[Operator, uuid.UUID] = FastAPIUsers[Operator, uuid.UUID](
    get_operator_manager_dep,
    [auth_backend],
)

current_active_operator = fastapi_users.current_user(active=True)
current_superuser_operator = fastapi_users.current_user(active=True, superuser=True)
