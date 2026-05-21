"""Unit tests for BatchedLogTransport.

We use httpx's MockTransport to assert on outbound payloads without spinning
up a real ingestion service.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from chatbot_sdk.transport import BatchedLogTransport


@pytest.fixture
def captured_requests() -> list[httpx.Request]:
    return []


@pytest.fixture
def mock_handler(captured_requests: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(202, json={"accepted": True})

    return handler


async def _make_transport(handler, **kwargs) -> BatchedLogTransport:
    transport = BatchedLogTransport(
        ingestion_url="http://ingestion.local/v1/logs",
        service="test",
        sdk_version="test",
        batch_max=kwargs.pop("batch_max", 4),
        flush_interval_s=kwargs.pop("flush_interval_s", 0.05),
        queue_max=kwargs.pop("queue_max", 100),
        **kwargs,
    )
    await transport.start()
    assert transport._client is not None
    transport._client._transport = httpx.MockTransport(handler)
    return transport


async def test_flushes_when_batch_max_reached(mock_handler, captured_requests):
    transport = await _make_transport(mock_handler, batch_max=3, flush_interval_s=10)
    for i in range(3):
        transport.submit({"i": i})
    await asyncio.sleep(0.1)
    await transport.close()

    assert len(captured_requests) == 1
    import json

    body = json.loads(captured_requests[0].content)
    assert body["service"] == "test"
    assert len(body["events"]) == 3
    assert transport.flushed_count == 3


async def test_flushes_on_interval_when_below_batch_max(mock_handler, captured_requests):
    transport = await _make_transport(mock_handler, batch_max=10, flush_interval_s=0.05)
    transport.submit({"i": 1})
    await asyncio.sleep(0.15)
    await transport.close()

    assert len(captured_requests) >= 1
    assert transport.flushed_count == 1


async def test_drops_oldest_on_queue_overflow(mock_handler):
    transport = await _make_transport(mock_handler, queue_max=3, flush_interval_s=10, batch_max=100)
    for i in range(5):
        transport.submit({"i": i})
    assert transport.dropped_count == 2
    assert len(transport._buffer) == 3
    # Oldest two should have been dropped.
    assert [e["i"] for e in transport._buffer] == [2, 3, 4]
    await transport.close()


async def test_retries_on_5xx_then_succeeds(captured_requests):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        captured_requests.append(request)
        if attempts["n"] < 2:
            return httpx.Response(503)
        return httpx.Response(202, json={"ok": True})

    transport = await _make_transport(
        handler,
        batch_max=1,
        flush_interval_s=10,
        max_retries=3,
        backoff_base_s=0.01,
    )
    transport.submit({"i": 1})
    await asyncio.sleep(0.2)
    await transport.close()

    assert attempts["n"] >= 2
    assert transport.flushed_count == 1
    assert transport.failed_batches == 0


async def test_does_not_retry_on_4xx_schema_error():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(422, json={"detail": "schema"})

    transport = await _make_transport(
        handler,
        batch_max=1,
        flush_interval_s=10,
        max_retries=5,
        backoff_base_s=0.01,
    )
    transport.submit({"i": 1})
    await asyncio.sleep(0.15)
    await transport.close()

    assert attempts["n"] == 1
    assert transport.failed_batches == 1
    assert transport.flushed_count == 0


async def test_close_drains_buffer(mock_handler, captured_requests):
    transport = await _make_transport(mock_handler, batch_max=100, flush_interval_s=10)
    for i in range(5):
        transport.submit({"i": i})
    await transport.close()

    # All 5 should have been shipped on close.
    total_events = sum(
        len(_parse_json(r.content)["events"]) for r in captured_requests
    )
    assert total_events == 5


def _parse_json(blob: bytes) -> dict:
    import json

    return json.loads(blob)
