"""Conversation memory — load the last N (role, content) turns from Postgres.

We deliberately keep this as a small, focused helper rather than reaching for
LangChain's ``ConversationBufferWindowMemory`` class hierarchy: our memory
lives in Postgres (the source of truth) and we just want a list of messages
to feed the LLM. The window size is configured globally.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from chat_service.config import settings
from chat_service.repositories import message_repository


async def load_window(
    session: AsyncSession, conversation_id: uuid.UUID
) -> list[tuple[str, str]]:
    """Return last N messages as ``(role, content)`` tuples in chrono order."""
    msgs = await message_repository.list_tail(
        session, conversation_id, n=settings.memory_window
    )
    return [(m.role, m.content) for m in msgs]
