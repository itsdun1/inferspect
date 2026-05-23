"""Unit tests for InferenceLogger spans and decorators."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from chatbot_sdk.client import InferenceLogger
from chatbot_sdk.schema import FinishReason, LogType, Status
from chatbot_sdk.transport import BatchedLogTransport


class _RecordingTransport(BatchedLogTransport):
    """In-memory replacement that captures submitted events without HTTP."""

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


@pytest.fixture
def logger():
    return InferenceLogger(
        ingestion_url="http://test.invalid",
        service="chat-service",
        transport=_RecordingTransport(),
    )


async def test_inference_span_emits_ok_log(logger):
    conv_id = uuid4()
    async with logger.inference(
        provider="google",
        model="gemini-2.5-pro",
        conversation_id=conv_id,
        input_preview="hi",
    ) as span:
        span.set_response(SimpleNamespace(
            content="hello back",
            usage_metadata={"input_tokens": 5, "output_tokens": 3},
            tool_calls=[],
        ))

    events = logger.transport.events  # type: ignore[attr-defined]
    assert len(events) == 1
    e = events[0]
    assert e["log_type"] == LogType.INFERENCE
    assert e["status"] == Status.OK
    assert e["finish_reason"] == FinishReason.STOP
    assert e["provider"] == "google"
    assert e["prompt_tokens"] == 5
    assert e["completion_tokens"] == 3
    assert e["total_tokens"] == 8
    assert e["conversation_id"] == str(conv_id)
    assert e["output_preview"] == "hello back"


async def test_inference_span_records_cancelled_on_cancellederror(logger):
    with pytest.raises(asyncio.CancelledError):
        async with logger.inference(provider="google", model="gemini-2.5-pro") as span:
            span.observe_chunk(SimpleNamespace(content="par", usage_metadata=None, tool_call_chunks=None))
            raise asyncio.CancelledError()

    events = logger.transport.events  # type: ignore[attr-defined]
    assert len(events) == 1
    assert events[0]["status"] == Status.CANCELLED
    assert events[0]["finish_reason"] == FinishReason.CANCELLED
    assert events[0]["output_preview"] == "par"


async def test_inference_span_records_error(logger):
    with pytest.raises(RuntimeError):
        async with logger.inference(provider="google", model="gemini-2.5-pro"):
            raise RuntimeError("boom")

    events = logger.transport.events  # type: ignore[attr-defined]
    assert events[0]["status"] == Status.ERROR
    assert events[0]["error_code"] == "RuntimeError"
    assert events[0]["error_message"] == "boom"


async def test_streaming_observe_chunk_captures_ttft(logger):
    async with logger.inference(
        provider="google", model="gemini-2.5-pro", stream=True
    ) as span:
        # simulate two chunks; ttft should be set on the first.
        await asyncio.sleep(0.01)
        span.observe_chunk(SimpleNamespace(content="he", usage_metadata=None, tool_call_chunks=None))
        await asyncio.sleep(0.01)
        span.observe_chunk(
            SimpleNamespace(
                content="llo",
                usage_metadata={"input_tokens": 4, "output_tokens": 2},
                tool_call_chunks=None,
            )
        )

    e = logger.transport.events[0]  # type: ignore[attr-defined]
    assert e["ttft_ms"] is not None and e["ttft_ms"] >= 0
    assert e["latency_ms"] >= e["ttft_ms"]
    assert e["output_preview"] == "hello"
    assert e["completion_tokens"] == 2


async def test_tool_traced_decorator_records_success_and_failure(logger):
    @logger.tool_traced(name="search")
    async def search(q: str) -> dict:
        return {"hits": [q]}

    @logger.tool_traced()
    async def boom():
        raise ValueError("nope")

    await search("cats")
    with pytest.raises(ValueError):
        await boom()

    events = logger.transport.events  # type: ignore[attr-defined]
    assert len(events) == 2
    ok, err = events
    assert ok["log_type"] == LogType.TOOL_EXECUTION
    assert ok["tool_name"] == "search"
    assert ok["status"] == Status.OK
    assert "cats" in ok["args_preview"]

    assert err["tool_name"] == "boom"
    assert err["status"] == Status.ERROR
    assert err["error_code"] == "ValueError"


async def test_inference_with_tool_calls_summary(logger):
    async with logger.inference(provider="openai", model="gpt-4.1") as span:
        span.set_response(
            SimpleNamespace(
                content="",
                usage_metadata={"input_tokens": 10, "output_tokens": 5},
                tool_calls=[
                    {"name": "get_weather", "args": {"city": "Tokyo"}},
                    {"name": "get_time", "args": {"tz": "Asia/Tokyo"}},
                ],
            )
        )

    e = logger.transport.events[0]  # type: ignore[attr-defined]
    assert e["tool_calls_count"] == 2
    assert {tc["name"] for tc in e["tool_calls_summary"]} == {"get_weather", "get_time"}
    assert e["finish_reason"] == FinishReason.TOOL_CALLS


async def test_inference_pii_redaction_on_previews(logger):
    """Default ``pii_redact=True`` redacts both input and output previews."""
    async with logger.inference(
        provider="openai",
        model="gpt-4.1",
        input_preview="please reach me at alice@example.com",
    ) as span:
        span.set_response(SimpleNamespace(
            content="your card 4111111111111111 was charged",
            usage_metadata={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
            tool_calls=[],
        ))

    e = logger.transport.events[0]  # type: ignore[attr-defined]
    assert "alice@example.com" not in e["input_preview"]
    assert "<EMAIL_ADDRESS>" in e["input_preview"]
    assert "4111111111111111" not in e["output_preview"]
    assert "<CREDIT_CARD>" in e["output_preview"]


async def test_tool_call_pii_redaction_on_args_and_result():
    log = InferenceLogger(
        ingestion_url="http://test.invalid",
        service="chat-service",
        transport=_RecordingTransport(),
    )
    async with log.tool_call(
        tool_name="lookup_user",
        args_preview='{"email": "alice@example.com"}',
    ) as span:
        span.set_result("user lives at 10.0.0.1")

    e = log.transport.events[0]  # type: ignore[attr-defined]
    assert "<EMAIL_ADDRESS>" in e["args_preview"]
    assert "alice@example.com" not in e["args_preview"]
    assert "<IPV4>" in e["result_preview"]
    assert "10.0.0.1" not in e["result_preview"]


async def test_inference_pii_redaction_disabled():
    log = InferenceLogger(
        ingestion_url="http://test.invalid",
        service="chat-service",
        pii_redact=False,
        transport=_RecordingTransport(),
    )
    async with log.inference(
        provider="openai",
        model="gpt-4.1",
        input_preview="alice@example.com",
    ) as span:
        span.set_response(SimpleNamespace(
            content="hi",
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            tool_calls=[],
        ))

    e = log.transport.events[0]  # type: ignore[attr-defined]
    assert e["input_preview"] == "alice@example.com"


async def test_from_env_reads_url_and_key(monkeypatch):
    monkeypatch.setenv("CHATBOT_SDK_URL", "http://ingest.test/v1/logs")
    monkeypatch.setenv("CHATBOT_SDK_KEY", "osk_test_abc")
    monkeypatch.setenv("CHATBOT_SDK_SERVICE", "my-service")
    log = InferenceLogger.from_env()
    assert log.service == "my-service"
    assert log.transport.api_key == "osk_test_abc"
    assert log.transport.ingestion_url == "http://ingest.test/v1/logs"


async def test_from_env_missing_url_raises(monkeypatch):
    monkeypatch.delenv("CHATBOT_SDK_URL", raising=False)
    with pytest.raises(ValueError):
        InferenceLogger.from_env()


async def test_aenter_aexit_lifecycle():
    transport = _RecordingTransport()
    log = InferenceLogger(
        ingestion_url="http://test.invalid",
        service="chat-service",
        transport=transport,
    )
    async with log as entered:
        assert entered is log


async def test_context_sets_and_resets_contextvar(logger):
    from chatbot_sdk.client import current_context

    conv = uuid4()
    sess = uuid4()
    assert current_context() == {}
    async with logger.context(conversation_id=conv, session_id=sess) as ctx:
        snapshot = current_context()
        assert snapshot["conversation_id"] == conv
        assert snapshot["session_id"] == sess
        assert ctx is snapshot
    assert current_context() == {}
