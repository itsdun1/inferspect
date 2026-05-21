"""Unit tests for ClickHouseWriter — covers the JSON-to-row projection."""

from __future__ import annotations

import json
from typing import Any

import pytest

from inference_consumer.repositories.clickhouse_writer import (
    INFERENCE_COLUMNS,
    TOOL_EXECUTION_COLUMNS,
    ClickHouseWriter,
)


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[list[Any]], list[str]]] = []

    async def insert(self, table: str, data: list[list[Any]], column_names: list[str]) -> None:
        self.calls.append((table, data, column_names))

    async def close(self) -> None:
        pass


async def test_inference_row_projection_in_column_order():
    client = _FakeClient()
    writer = ClickHouseWriter(client, table="inference_logs", columns=INFERENCE_COLUMNS)

    event = {
        "request_id": "00000000-0000-0000-0000-000000000001",
        "conversation_id": "00000000-0000-0000-0000-0000000000aa",
        "service": "chat-service",
        "provider": "google",
        "model": "gemini-2.5-pro",
        "started_at": "2026-05-22T00:00:00Z",
        "finished_at": "2026-05-22T00:00:01Z",
        "received_at": "2026-05-22T00:00:02Z",
        "latency_ms": 1000,
        "stream": True,  # bool → 1
        "status": "ok",
        "tool_calls_summary": [{"name": "x"}],
        "metadata": {"k": "v"},
    }

    await writer.insert_batch([event])

    assert len(client.calls) == 1
    table, rows, cols = client.calls[0]
    assert table == "inference_logs"
    assert cols == INFERENCE_COLUMNS
    assert len(rows) == 1
    row = rows[0]

    # Cross-check by index.
    assert row[cols.index("provider")] == "google"
    assert row[cols.index("stream")] == 1            # bool coerced
    assert row[cols.index("metadata")] == json.dumps({"k": "v"})
    assert row[cols.index("tool_calls_summary")] == json.dumps([{"name": "x"}])
    assert row[cols.index("ttft_ms")] is None        # missing → None for nullable
    assert row[cols.index("user_id")] is None


async def test_tool_execution_row_projection():
    client = _FakeClient()
    writer = ClickHouseWriter(client, table="tool_executions", columns=TOOL_EXECUTION_COLUMNS)

    event = {
        "request_id": "00000000-0000-0000-0000-000000000001",
        "tool_call_id": "00000000-0000-0000-0000-000000000002",
        "service": "chat-service",
        "tool_name": "search",
        "started_at": "2026-05-22T00:00:00Z",
        "finished_at": "2026-05-22T00:00:01Z",
        "latency_ms": 12,
        "status": "ok",
    }

    await writer.insert_batch([event])

    table, rows, cols = client.calls[0]
    assert table == "tool_executions"
    assert cols == TOOL_EXECUTION_COLUMNS
    assert rows[0][cols.index("tool_name")] == "search"
    assert rows[0][cols.index("result_size_bytes")] is None or rows[0][cols.index("result_size_bytes")] == 0


async def test_empty_batch_is_noop():
    client = _FakeClient()
    writer = ClickHouseWriter(client, table="inference_logs", columns=INFERENCE_COLUMNS)
    await writer.insert_batch([])
    assert client.calls == []
