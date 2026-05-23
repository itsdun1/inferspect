"""Cross-tenant read functions in admin_service — direct unit tests against
sqlite-in-memory. These are the same shape as the chat-service tests we
deleted; admin lives in insights-api now.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from insights_api.db.models import Base, Conversation, SharedBase, User
from insights_api.services import admin_service


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # Create both owned (operators) and shared (users/conversations)
        # tables so the read paths have something to query.
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(SharedBase.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


async def test_list_all_users_returns_every_user(session: AsyncSession):
    a = User(email="a@x", hashed_password="", role="user")
    b = User(email="b@x", hashed_password="", role="admin", is_superuser=True)
    session.add_all([a, b])
    await session.flush()

    users = await admin_service.list_all_users(session)
    assert {u.email for u in users} == {"a@x", "b@x"}


async def test_list_all_conversations_returns_cross_tenant(session: AsyncSession):
    a = User(email="alice@x", hashed_password="", role="user")
    b = User(email="bob@x", hashed_password="", role="user")
    session.add_all([a, b])
    await session.flush()

    session.add_all(
        [
            Conversation(user_id=a.id, title="A1"),
            Conversation(user_id=a.id, title="A2"),
            Conversation(user_id=b.id, title="B1"),
        ]
    )
    await session.flush()

    convs = await admin_service.list_all_conversations(session)
    assert {c.title for c in convs} == {"A1", "A2", "B1"}


async def test_set_user_role_promotes_and_demotes(session: AsyncSession):
    u = User(email="c@x", hashed_password="", role="user")
    session.add(u)
    await session.flush()

    promoted = await admin_service.set_user_role(session, u.id, role="admin")
    assert promoted.role == "admin"
    assert promoted.is_superuser is True

    demoted = await admin_service.set_user_role(session, u.id, role="user")
    assert demoted.role == "user"
    assert demoted.is_superuser is False


async def test_set_user_role_rejects_invalid(session: AsyncSession):
    u = User(email="d@x", hashed_password="", role="user")
    session.add(u)
    await session.flush()

    with pytest.raises(ValueError):
        await admin_service.set_user_role(session, u.id, role="root")


async def test_set_user_role_not_found(session: AsyncSession):
    with pytest.raises(LookupError):
        await admin_service.set_user_role(session, uuid.uuid4(), role="admin")


async def test_generate_synthetic_logs_pushes_through_sdk_transport(session: AsyncSession):
    """Hand the synthetic generator a fake SDK with a recording transport
    and assert it submits the right counts of envelopes."""

    submitted: list[dict] = []

    class _RecordingTransport:
        def submit(self, event: dict) -> None:
            submitted.append(event)

    class _FakeSDK:
        transport = _RecordingTransport()

    result = await admin_service.generate_synthetic_logs(
        _FakeSDK(),
        count=10,
        error_rate=0.0,
        tool_call_rate=1.0,  # force tool events
        spread_seconds=0,
    )
    assert result["inference_events"] == 10
    assert result["tool_events"] == 10
    # 10 inference + 10 tool envelopes submitted
    assert len(submitted) == 20
    log_types = {e["log_type"] for e in submitted}
    assert log_types == {"inference", "tool_execution"}
