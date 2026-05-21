"""ChatService end-to-end with a fake LangGraph agent.

We monkey-patch ``build_agent`` so the test doesn't need a real LLM. The full
path through repositories + agent invocation + assistant message persistence
runs for real against SQLite in-memory.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from chat_service.db.models import Base
from chat_service.llm import agent as agent_module
from chat_service.repositories import user_repository
from chat_service.services.chat_service import ChatService


class _FakeAgent:
    """Looks like a compiled LangGraph: has ``ainvoke``."""

    def __init__(self, reply: str = "hello, world"):
        self._reply = reply

    async def ainvoke(self, state, config=None):
        messages = list(state.get("messages", []))
        messages.append(AIMessage(
            content=self._reply,
            usage_metadata={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        ))
        return {"messages": messages}


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


async def test_send_message_creates_conversation_and_two_messages(session: AsyncSession, monkeypatch):
    def fake_build_agent(model_name, tools=None):
        return _FakeAgent(reply="hello, world")

    monkeypatch.setattr(agent_module, "build_agent", fake_build_agent)
    # The service imports build_agent at the module level — patch the local ref too.
    import chat_service.services.chat_service as cs
    monkeypatch.setattr(cs, "build_agent", fake_build_agent)

    user = await user_repository.get_or_create_by_email(session, email="t@x")
    svc = ChatService(logger=None)

    resp = await svc.send_message(
        session=session,
        user=user,
        conversation_id=None,
        content="hi",
        model="gemini-2.5-pro",
    )

    assert resp.user_message.role == "user"
    assert resp.user_message.content == "hi"
    assert resp.assistant_message.role == "assistant"
    assert resp.assistant_message.content == "hello, world"
