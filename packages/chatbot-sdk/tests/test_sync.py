"""Tests for ``SyncInferenceLogger``."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from chatbot_sdk.schema import LogType, Status
from chatbot_sdk.sync import SyncInferenceLogger
from chatbot_sdk.transport import BatchedLogTransport


class _RecordingTransport(BatchedLogTransport):
    """Captures submitted events without HTTP. Mirrors the async-stub in
    test_client.py — the daemon-loop thread still calls ``start()`` /
    ``close()`` on us, so they must be coroutine-safe no-ops."""

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


def test_sync_inference_span_emits_log():
    transport = _RecordingTransport()
    logger = SyncInferenceLogger(
        ingestion_url="http://test.invalid",
        service="sync-test",
        transport=transport,
    )
    conv_id = uuid4()
    with logger:
        with logger.inference(
            provider="openai",
            model="gpt-4o-mini",
            conversation_id=conv_id,
            input_preview="hi",
        ) as span:
            span.set_response(
                SimpleNamespace(
                    content="hello back",
                    usage_metadata={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
                    tool_calls=[],
                )
            )

    assert len(transport.events) == 1
    e = transport.events[0]
    assert e["log_type"] == LogType.INFERENCE
    assert e["status"] == Status.OK
    assert e["provider"] == "openai"
    assert e["prompt_tokens"] == 5
    assert e["completion_tokens"] == 3
    assert e["output_preview"] == "hello back"
    assert e["conversation_id"] == str(conv_id)


def test_sync_tool_call_emits_log():
    transport = _RecordingTransport()
    logger = SyncInferenceLogger(
        ingestion_url="http://test.invalid",
        service="sync-test",
        transport=transport,
    )
    with logger:
        with logger.tool_call(tool_name="get_time", args_preview="{}") as span:
            span.set_result({"now": "2026-05-21T00:00:00Z"})

    assert len(transport.events) == 1
    e = transport.events[0]
    assert e["log_type"] == LogType.TOOL_EXECUTION
    assert e["tool_name"] == "get_time"
    assert e["status"] == Status.OK


def test_sync_inference_span_records_error():
    transport = _RecordingTransport()
    logger = SyncInferenceLogger(
        ingestion_url="http://test.invalid",
        service="sync-test",
        transport=transport,
    )
    with logger:
        with pytest.raises(RuntimeError):
            with logger.inference(provider="openai", model="gpt-4o-mini"):
                raise RuntimeError("boom")

    assert len(transport.events) == 1
    e = transport.events[0]
    assert e["status"] == Status.ERROR
    assert e["error_code"] == "RuntimeError"


def test_sync_from_env_requires_url(monkeypatch):
    monkeypatch.delenv("CHATBOT_SDK_URL", raising=False)
    with pytest.raises(ValueError):
        SyncInferenceLogger.from_env()


def test_sync_double_start_and_close_is_safe():
    transport = _RecordingTransport()
    logger = SyncInferenceLogger(
        ingestion_url="http://test.invalid",
        service="sync-test",
        transport=transport,
    )
    logger.start()
    logger.start()  # idempotent
    logger.close()
    logger.close()  # idempotent
