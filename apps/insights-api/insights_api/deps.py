"""FastAPI dependency injection for insights-api.

The async ClickHouse client is created lazily by a factory stored on
``app.state`` — ``clickhouse_connect.get_async_client(...)`` is itself a
coroutine, so we can't call it from synchronous startup. The factory is
invoked once on first use and the resulting client is cached for the
lifetime of the process.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Request


class ClickHouseClientHolder:
    """Lazy holder around the async clickhouse-connect client.

    Multiple concurrent requests during startup race the same factory once;
    a lock serializes the first await so we end up with a single client.
    """

    def __init__(self, factory) -> None:
        self._factory = factory
        self._client: Any | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                self._client = await self._factory()
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None


def get_holder(request: Request) -> ClickHouseClientHolder:
    return request.app.state.ch_holder  # type: ignore[no-any-return]


async def get_ch_client(request: Request) -> Any:
    holder: ClickHouseClientHolder = request.app.state.ch_holder
    return await holder.get()
