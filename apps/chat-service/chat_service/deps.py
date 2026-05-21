"""FastAPI dependencies.

Real auth via ``fastapi-users`` (cookie-based JWT). Repositories and services
still accept ``session`` and ``user`` as plain arguments — the auth swap
didn't touch their code, which is the point of the layered pattern."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from chat_service.auth.deps import current_active_user, current_admin_user
from chat_service.db.models import User
from chat_service.db.session import get_session
from chat_service.services import chat_service, conversation_service


# ─── Session (transactional) ─────────────────────────────────────
SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ─── Current user (real auth) ────────────────────────────────────
CurrentUserDep = Annotated[User, Depends(current_active_user)]
AdminUserDep = Annotated[User, Depends(current_admin_user)]


# ─── Service handles ─────────────────────────────────────────────
def get_chat_service(request: Request) -> chat_service.ChatService:
    return request.app.state.chat_service  # type: ignore[no-any-return]


def get_conversation_service(request: Request) -> conversation_service.ConversationService:
    return request.app.state.conversation_service  # type: ignore[no-any-return]


ChatServiceDep = Annotated[chat_service.ChatService, Depends(get_chat_service)]
ConversationServiceDep = Annotated[
    conversation_service.ConversationService, Depends(get_conversation_service)
]
