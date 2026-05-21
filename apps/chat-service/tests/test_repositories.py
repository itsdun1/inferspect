"""Smoke tests for the repository + service layers against SQLite in-memory.

These verify the layered pattern wires up — the session is owned by the
test, passed into every repository and service call. No HTTP, no LLM."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from chat_service.db.models import Base
from chat_service.repositories import (
    conversation_repository,
    message_repository,
    user_repository,
)
from chat_service.services.conversation_service import ConversationService


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


async def test_create_user_and_conversation(session):
    user = await user_repository.get_or_create_by_email(session, email="alice@example.com")
    assert user.role == "user"
    assert user.id is not None

    convo = await conversation_repository.create(
        session, user_id=user.id, title="hello", model="gemini-2.5-pro"
    )
    assert convo.status == "active"
    assert convo.message_count == 0


async def test_message_create_and_list(session):
    user = await user_repository.get_or_create_by_email(session, email="b@example.com")
    convo = await conversation_repository.create(session, user_id=user.id)

    await message_repository.create(
        session, conversation_id=convo.id, role="user", content="hi"
    )
    await message_repository.create(
        session, conversation_id=convo.id, role="assistant", content="hello back"
    )

    msgs = await message_repository.list_for_conversation(session, convo.id)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert [m.content for m in msgs] == ["hi", "hello back"]


async def test_conversation_service_list_excludes_other_users(session):
    svc = ConversationService()
    alice = await user_repository.get_or_create_by_email(session, email="alice@x")
    bob = await user_repository.get_or_create_by_email(session, email="bob@x")

    await conversation_repository.create(session, user_id=alice.id, title="A1")
    await conversation_repository.create(session, user_id=alice.id, title="A2")
    await conversation_repository.create(session, user_id=bob.id, title="B1")

    alice_convos = await svc.list_for_user(session=session, user=alice)
    bob_convos = await svc.list_for_user(session=session, user=bob)
    assert {c.title for c in alice_convos} == {"A1", "A2"}
    assert {c.title for c in bob_convos} == {"B1"}


async def test_cancel_sets_status(session):
    svc = ConversationService()
    user = await user_repository.get_or_create_by_email(session, email="c@x")
    convo = await conversation_repository.create(session, user_id=user.id)

    cancelled = await svc.cancel(session=session, user=user, conversation_id=convo.id)
    assert cancelled.status == "cancelled"
