"""Unit tests for BatchService — uses in-memory fakes for the writer and DLQ
so the test runs without ClickHouse / Valkey."""

from __future__ import annotations

import json
from typing import Any

import pytest

from inference_consumer.services.batch_service import BatchService


class _FakeWriter:
    def __init__(self, fail: bool = False) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self._fail = fail

    async def insert_batch(self, events: list[dict[str, Any]]) -> None:
        if self._fail:
            raise RuntimeError("ch unreachable")
        self.batches.append(events)


class _FakeDLQ:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, Any]], str]] = []

    async def publish_failed(self, events: list[dict[str, Any]], *, error: str) -> None:
        self.calls.append((events, error))


def _wrap(event: dict[str, Any]) -> dict[str, Any]:
    """Simulate the publisher's envelope ``{"payload": "<json>"}``."""
    return {"payload": json.dumps(event)}


async def test_happy_path_writes_all_events():
    writer, dlq = _FakeWriter(), _FakeDLQ()
    svc = BatchService(writer=writer, dlq=dlq, kind="inference")

    events = [{"log_type": "inference", "request_id": str(i)} for i in range(3)]
    written = await svc.handle_batch([_wrap(e) for e in events])

    assert written == 3
    assert len(writer.batches) == 1
    assert writer.batches[0] == events
    assert dlq.calls == []


async def test_malformed_messages_go_to_dlq():
    writer, dlq = _FakeWriter(), _FakeDLQ()
    svc = BatchService(writer=writer, dlq=dlq, kind="inference")

    written = await svc.handle_batch(["not-json", _wrap({"log_type": "inference", "request_id": "x"})])

    assert written == 1
    assert dlq.calls and dlq.calls[0][1] == "malformed-message"


async def test_insert_failure_routes_batch_to_dlq():
    writer, dlq = _FakeWriter(fail=True), _FakeDLQ()
    svc = BatchService(writer=writer, dlq=dlq, kind="inference")

    written = await svc.handle_batch([_wrap({"log_type": "inference", "request_id": "x"})])

    assert written == 0
    assert len(dlq.calls) == 1
    assert "RuntimeError" in dlq.calls[0][1]


async def test_handles_bare_event_dict():
    """Some FastStream configurations hand us the parsed event directly."""
    writer, dlq = _FakeWriter(), _FakeDLQ()
    svc = BatchService(writer=writer, dlq=dlq, kind="inference")

    bare = {"log_type": "inference", "request_id": "y"}
    written = await svc.handle_batch([bare])  # type: ignore[list-item]

    assert written == 1
    assert writer.batches[0] == [bare]
