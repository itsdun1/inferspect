"""Mirror of the inference-consumer batch tests."""

from __future__ import annotations

import json
from typing import Any

from app_log_consumer.services.batch_service import BatchService


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
    return {"payload": json.dumps(event)}


async def test_writes_application_events():
    writer, dlq = _FakeWriter(), _FakeDLQ()
    svc = BatchService(writer=writer, dlq=dlq)

    events = [
        {"ts": "2026-05-22T00:00:00Z", "level": "INFO", "service": "chat", "message": f"m{i}"}
        for i in range(2)
    ]
    n = await svc.handle_batch([_wrap(e) for e in events])
    assert n == 2
    assert writer.batches[0] == events


async def test_routes_failures_to_dlq():
    svc = BatchService(writer=_FakeWriter(fail=True), dlq=_FakeDLQ())
    n = await svc.handle_batch([_wrap({"ts": "2026-05-22T00:00:00Z", "level": "INFO", "service": "chat", "message": "x"})])
    assert n == 0
