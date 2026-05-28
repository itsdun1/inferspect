"""Repository: the only layer that touches ClickHouse.

Wraps ``clickhouse-connect``'s async client. Two tables, one writer object
per table — same shape, different schema mapping.
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, Union

logger = logging.getLogger(__name__)


class _AsyncCHClient(Protocol):
    async def insert(self, table: str, data: list[list[Any]], column_names: list[str]) -> Any: ...
    async def close(self) -> None: ...


ClientFactory = Callable[[], Union[_AsyncCHClient, Awaitable[_AsyncCHClient]]]


# Columns for each table in the same order as the SQL DDL in
# infra/clickhouse/init.sql. Keep these in sync if the DDL changes.
INFERENCE_COLUMNS: list[str] = [
    "request_id",
    "conversation_id",
    "session_id",
    "user_id",
    "service",
    "provider",
    "model",
    "started_at",
    "finished_at",
    "received_at",
    "latency_ms",
    "ttft_ms",
    "stream",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cost_usd",
    "status",
    "finish_reason",
    "tool_calls_count",
    "tool_calls_summary",
    "error_code",
    "error_message",
    "input_preview",
    "output_preview",
    "metadata",
    "client",
    "source",
    "host_id",
    "process_id",
    "container_id",
    "fingerprint",
]

TOOL_EXECUTION_COLUMNS: list[str] = [
    "request_id",
    "tool_call_id",
    "parent_inference_request_id",
    "conversation_id",
    "session_id",
    "user_id",
    "service",
    "tool_name",
    "started_at",
    "finished_at",
    "received_at",
    "latency_ms",
    "status",
    "error_code",
    "error_message",
    "args_preview",
    "result_preview",
    "result_size_bytes",
    "metadata",
    "client",
    "source",
    "host_id",
    "process_id",
    "container_id",
]


class ClickHouseWriter:
    """Bulk-insert log events into a ClickHouse table.

    Schema mapping is done here: convert the JSON dict the SDK emitted into a
    row in the canonical column order. Missing fields use safe defaults.

    Accepts either a ready ``_AsyncCHClient`` or a ``client_factory`` that
    produces one (sync or async). The factory is invoked lazily so the
    consumer's startup code doesn't have to be async.
    """

    def __init__(
        self,
        client_or_factory: _AsyncCHClient | ClientFactory,
        *,
        table: str,
        columns: list[str],
    ) -> None:
        if callable(client_or_factory) and not hasattr(client_or_factory, "insert"):
            self._factory: ClientFactory | None = client_or_factory  # type: ignore[assignment]
            self._client: _AsyncCHClient | None = None
        else:
            self._factory = None
            self._client = client_or_factory  # type: ignore[assignment]
        self._table = table
        self._columns = columns

    async def _get_client(self) -> _AsyncCHClient:
        if self._client is None:
            assert self._factory is not None
            produced = self._factory()
            if inspect.isawaitable(produced):
                produced = await produced
            self._client = produced  # type: ignore[assignment]
        return self._client  # type: ignore[return-value]

    async def insert_batch(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        client = await self._get_client()
        rows = [self._to_row(e) for e in events]
        await client.insert(self._table, rows, column_names=self._columns)

    def _to_row(self, event: dict[str, Any]) -> list[Any]:
        return [_coerce(event, col) for col in self._columns]


def _coerce(event: dict[str, Any], col: str) -> Any:
    """Project an SDK JSON event onto a ClickHouse column value.

    Unknown columns return safe defaults rather than raising — that way a
    forward-compatible SDK can omit fields we don't yet track."""
    value = event.get(col)
    # JSON-encoded columns (stored as String in ClickHouse).
    if col in {"metadata", "tool_calls_summary"}:
        if value is None:
            return "{}" if col == "metadata" else "[]"
        if isinstance(value, str):
            return value
        return json.dumps(value, default=str)
    # UUID columns: ClickHouse driver accepts string form.
    if value is None and col in {"conversation_id", "session_id", "user_id", "ttft_ms",
                                  "error_code", "error_message", "trace_id", "span_id",
                                  "parent_inference_request_id"}:
        return None
    # Tenant tag — non-Nullable in CH, default empty string when absent
    # (e.g. older events from before Phase C, or local dev with no key map).
    if col == "client" and value is None:
        return ""
    # Phase G origin tags. Non-Nullable in CH except process_id; default
    # empty string when older SDK rows don't carry them.
    if col in {"source", "host_id", "container_id", "fingerprint"} and value is None:
        return "" if col != "source" else "sdk"
    # finish_reason is LowCardinality(String) DEFAULT '' — non-Nullable in CH.
    # The Pydantic field is Optional, so events that didn't conclude with a
    # known reason serialize to None. Convert to the empty default.
    if col == "finish_reason" and value is None:
        return ""
    # Boolean → UInt8.
    if isinstance(value, bool):
        return 1 if value else 0
    # Default: return value as-is.
    return value
