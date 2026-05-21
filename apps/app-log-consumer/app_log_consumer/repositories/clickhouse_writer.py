"""ClickHouse writer for application_logs."""

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


APPLICATION_COLUMNS: list[str] = [
    "ts",
    "received_at",
    "level",
    "service",
    "logger",
    "trace_id",
    "span_id",
    "message",
    "attributes",
]


class ApplicationLogsWriter:
    def __init__(
        self,
        client_or_factory: _AsyncCHClient | ClientFactory,
        *,
        table: str = "application_logs",
    ) -> None:
        if callable(client_or_factory) and not hasattr(client_or_factory, "insert"):
            self._factory: ClientFactory | None = client_or_factory  # type: ignore[assignment]
            self._client: _AsyncCHClient | None = None
        else:
            self._factory = None
            self._client = client_or_factory  # type: ignore[assignment]
        self._table = table

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
        rows = [[_coerce(e, col) for col in APPLICATION_COLUMNS] for e in events]
        await client.insert(self._table, rows, column_names=APPLICATION_COLUMNS)


def _coerce(event: dict[str, Any], col: str) -> Any:
    value = event.get(col)
    if col == "attributes":
        if value is None:
            return "{}"
        if isinstance(value, str):
            return value
        return json.dumps(value, default=str)
    return value
