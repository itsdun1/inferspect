"""Active-stream registry — tracks in-flight chat tasks so they can be cancelled.

A single process holds one ``StreamRegistry``. The chat-service stores it on
``app.state`` at startup. When ``POST /chat`` starts streaming, it registers
the current task under ``conversation_id``. ``POST /conversations/{id}/cancel``
looks the task up and calls ``task.cancel()``.

If we scale chat-service to multiple replicas, cancel needs to fan out via
Valkey pub/sub — but for the demo a single process is enough.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


class StreamRegistry:
    def __init__(self) -> None:
        self._tasks: dict[uuid.UUID, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def register_current_task(self, conversation_id: uuid.UUID) -> AsyncIterator[None]:
        """Register ``asyncio.current_task()`` against ``conversation_id`` for the
        duration of the with-block. Yields ``None`` and unregisters on exit."""
        task = asyncio.current_task()
        if task is None:
            yield
            return
        async with self._lock:
            existing = self._tasks.get(conversation_id)
            if existing is not None and not existing.done():
                # A previous in-flight stream is still running. Cancel it so the
                # new request takes over cleanly (last-write-wins).
                existing.cancel()
            self._tasks[conversation_id] = task
        try:
            yield
        finally:
            async with self._lock:
                if self._tasks.get(conversation_id) is task:
                    del self._tasks[conversation_id]

    async def cancel(self, conversation_id: uuid.UUID) -> bool:
        """Cancel the in-flight stream for a conversation. Returns ``True`` if
        a task was found and cancelled, ``False`` if there was nothing to cancel."""
        async with self._lock:
            task = self._tasks.get(conversation_id)
        if task is None or task.done():
            return False
        task.cancel()
        logger.info("cancelled stream for conversation %s", conversation_id)
        return True

    def has_active(self, conversation_id: uuid.UUID) -> bool:
        task = self._tasks.get(conversation_id)
        return task is not None and not task.done()
