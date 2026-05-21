"""Batched non-blocking HTTP transport for shipping logs to the ingestion service.

Design constraints:
 - Logging must never block the chat path. A full queue drops oldest events.
 - Retries: bounded exponential backoff with jitter. On final failure, drop and
   increment a counter — never raise into the caller's stack.
 - Batching: flush whenever batch_max events are buffered OR flush_interval_s
   has elapsed since the oldest event, whichever first.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import deque
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class BatchedLogTransport:
    """Background flusher that ships log events in batches over HTTP."""

    def __init__(
        self,
        ingestion_url: str,
        *,
        service: str,
        sdk_version: str,
        api_key: str | None = None,
        batch_max: int = 64,
        flush_interval_s: float = 1.0,
        queue_max: int = 10_000,
        request_timeout_s: float = 5.0,
        max_retries: int = 3,
        backoff_base_s: float = 0.5,
    ) -> None:
        self.ingestion_url = ingestion_url
        self.service = service
        self.sdk_version = sdk_version
        self.api_key = api_key
        self.batch_max = batch_max
        self.flush_interval_s = flush_interval_s
        self.queue_max = queue_max
        self.request_timeout_s = request_timeout_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s

        # Bounded deque is faster than asyncio.Queue and supports drop-oldest.
        self._buffer: deque[dict[str, Any]] = deque(maxlen=queue_max)
        self._flush_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._client: httpx.AsyncClient | None = None
        self._closed = False
        # Diagnostic counters.
        self.dropped_count = 0
        self.flushed_count = 0
        self.failed_batches = 0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._client = httpx.AsyncClient(timeout=self.request_timeout_s)
        self._task = asyncio.create_task(self._flusher(), name="chatbot-sdk-flusher")

    async def close(self) -> None:
        """Flush remaining events and stop the background task."""
        self._closed = True
        self._flush_event.set()
        if self._task is not None:
            await self._task
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def submit(self, event: dict[str, Any]) -> None:
        """Enqueue a serialized log event. Non-blocking; drops oldest on overflow."""
        if self._closed:
            return
        if len(self._buffer) == self.queue_max:
            self._buffer.popleft()
            self.dropped_count += 1
        self._buffer.append(event)
        if len(self._buffer) >= self.batch_max:
            self._flush_event.set()

    async def _flusher(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._flush_event.wait(), timeout=self.flush_interval_s)
            except asyncio.TimeoutError:
                pass
            self._flush_event.clear()

            if not self._buffer and not self._closed:
                continue

            batch: list[dict[str, Any]] = []
            while self._buffer and len(batch) < self.batch_max:
                batch.append(self._buffer.popleft())

            if batch:
                await self._send(batch)

            if self._closed and not self._buffer:
                break

    async def _send(self, events: list[dict[str, Any]]) -> None:
        assert self._client is not None
        envelope = {
            "service": self.service,
            "sdk_version": self.sdk_version,
            "events": events,
        }
        headers: dict[str, str] = {"content-type": "application/json"}
        if self.api_key:
            headers["x-sdk-key"] = self.api_key

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await self._client.post(self.ingestion_url, json=envelope, headers=headers)
                if 200 <= resp.status_code < 300:
                    self.flushed_count += len(events)
                    return
                # 4xx schema errors: don't retry — they won't succeed.
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    logger.warning(
                        "ingestion rejected batch (%d) status=%d body=%s",
                        len(events),
                        resp.status_code,
                        resp.text[:500],
                    )
                    self.failed_batches += 1
                    return
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                logger.debug("ingestion attempt %d failed: %s", attempt, exc)

            if attempt < self.max_retries:
                delay = self.backoff_base_s * (2 ** (attempt - 1))
                delay += random.uniform(0, delay)  # jitter
                await asyncio.sleep(delay)

        # All attempts failed.
        self.failed_batches += 1
        logger.warning("ingestion dropped batch of %d after %d retries", len(events), self.max_retries)

    # ─── Diagnostics ─────────────────────────────────────────────
    def stats(self) -> dict[str, int]:
        return {
            "queued": len(self._buffer),
            "dropped": self.dropped_count,
            "flushed": self.flushed_count,
            "failed_batches": self.failed_batches,
        }
