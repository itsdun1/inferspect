"""Shared test fixtures: a fake ClickHouse client that records calls and
returns scripted rows. Lets every service-level test run without ClickHouse.
"""

from __future__ import annotations

from typing import Any


class _FakeQueryResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def named_results(self):
        return iter(self._rows)


class FakeCHClient:
    """Records queries; returns scripted results in FIFO order.

    Usage::

        client = FakeCHClient([rows_for_first_query, rows_for_second_query])
        client.queue_result(more_rows)  # also fine
        await some_service.func(client, ...)
        assert len(client.calls) == 2
    """

    def __init__(self, results: list[list[dict[str, Any]]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._results: list[list[dict[str, Any]]] = list(results or [])

    def queue_result(self, rows: list[dict[str, Any]]) -> None:
        self._results.append(rows)

    async def query(self, query: str, parameters: dict[str, Any] | None = None):
        self.calls.append((query, parameters or {}))
        rows = self._results.pop(0) if self._results else []
        return _FakeQueryResult(rows)

    async def close(self) -> None:
        pass
