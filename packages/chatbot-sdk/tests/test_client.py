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
