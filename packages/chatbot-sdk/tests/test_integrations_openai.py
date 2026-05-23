"""Tests for the OpenAI auto-instrumentation.

We use a fake AsyncOpenAI-shaped client (no network) and verify:
  - one async ``create()`` call emits exactly one inference log
  - usage/tokens land on the captured event
  - calling ``instrument()`` twice does not double-wrap
  - sync clients raise NotImplementedError pointing at SyncInferenceLogger
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from chatbot_sdk.client import InferenceLogger
from chatbot_sdk.integrations.openai import instrument
from chatbot_sdk.schema import LogType, Status
from chatbot_sdk.transport import BatchedLogTransport


class _RecordingTransport(BatchedLogTransport):
    def __init__(self) -> None:
        super().__init__(
            ingestion_url="http://test.invalid",
            service="test",
            sdk_version="test",
        )
        self.events: list[dict] = []

    def submit(self, event: dict) -> None:  # type: ignore[override]
        self.events.append(event)

    async def start(self) -> None:  # type: ignore[override]
        pass

    async def close(self) -> None:  # type: ignore[override]
        pass


def _fake_response(content: str = "hi", prompt_tokens: int = 7, completion_tokens: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        return _fake_response()


class _FakeChat:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()


class _FakeAsyncOpenAI:
    def __init__(self) -> None:
        self.chat = _FakeChat()


class _FakeSyncCompletions:
    def create(self, **kwargs: Any) -> Any:  # sync — NOT async
        return _fake_response()


class _FakeSyncChat:
    def __init__(self) -> None:
        self.completions = _FakeSyncCompletions()


class _FakeSyncOpenAI:
    def __init__(self) -> None:
        self.chat = _FakeSyncChat()


@pytest.fixture
def logger():
    return InferenceLogger(
        ingestion_url="http://test.invalid",
        service="chat-service",
        transport=_RecordingTransport(),
    )


async def test_instrument_async_emits_one_inference_event(logger):
    client = _FakeAsyncOpenAI()
    instrument(client, logger=logger)
    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hello"}],
    )
    assert resp.choices[0].message.content == "hi"

    events = logger.transport.events  # type: ignore[attr-defined]
    assert len(events) == 1
    e = events[0]
    assert e["log_type"] == LogType.INFERENCE
    assert e["status"] == Status.OK
    assert e["provider"] == "openai"
    assert e["model"] == "gpt-4o-mini"
    assert e["prompt_tokens"] == 7
    assert e["completion_tokens"] == 3
    assert e["total_tokens"] == 10


async def test_instrument_is_idempotent(logger):
    """Calling instrument() twice still produces only one event per call."""
    client = _FakeAsyncOpenAI()
    instrument(client, logger=logger)
    instrument(client, logger=logger)  # no-op
    await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "ping"}],
    )
    events = logger.transport.events  # type: ignore[attr-defined]
    assert len(events) == 1
    # The fake completions tracked one underlying call, not two.
    assert client.chat.completions.calls == 1


async def test_instrument_sync_raises_not_implemented(logger):
    """Sync clients are out of scope; users must use SyncInferenceLogger."""
    client = _FakeSyncOpenAI()
    instrument(client, logger=logger)
    with pytest.raises(NotImplementedError) as exc:
        client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert "SyncInferenceLogger" in str(exc.value)


async def test_instrument_reads_current_context_for_conversation_id(logger):
    """conversation_id from logger.context() flows onto the emitted event."""
    import uuid

    client = _FakeAsyncOpenAI()
    instrument(client, logger=logger)
    conv = uuid.uuid4()
    async with logger.context(conversation_id=conv):
        await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
        )
    events = logger.transport.events  # type: ignore[attr-defined]
    assert len(events) == 1
    assert events[0]["conversation_id"] == str(conv)


async def test_instrument_captures_tool_calls(logger):
    """tool_calls in the response are summarized on the event."""
    import json

    class _ToolCallsClient:
        class _C:
            class _Comp:
                async def create(self, **kwargs: Any) -> Any:
                    return SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                message=SimpleNamespace(
                                    content=None,
                                    tool_calls=[
                                        SimpleNamespace(
                                            function=SimpleNamespace(
                                                name="search",
                                                arguments=json.dumps({"q": "cats"}),
                                            )
                                        )
                                    ],
                                )
                            )
                        ],
                        usage=SimpleNamespace(
                            prompt_tokens=4, completion_tokens=2, total_tokens=6
                        ),
                    )

            completions = _Comp()

        chat = _C()

    client = _ToolCallsClient()
    instrument(client, logger=logger)
    await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "find cats"}],
    )
    events = logger.transport.events  # type: ignore[attr-defined]
    assert len(events) == 1
    assert events[0]["tool_calls_count"] == 1
    assert events[0]["tool_calls_summary"][0]["name"] == "search"
