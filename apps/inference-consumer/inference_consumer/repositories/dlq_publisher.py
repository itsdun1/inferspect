"""Repository: republish a batch of failed events to a DLQ stream."""

from __future__ import annotations

import json
import logging
from typing import Any

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class DLQPublisher:
    def __init__(self, client: Redis, *, stream: str, maxlen: int = 100_000) -> None:
        self._client = client
        self._stream = stream
        self._maxlen = maxlen

    async def publish_failed(self, events: list[dict[str, Any]], *, error: str) -> None:
        if not events:
            return
        for ev in events:
            payload = {"payload": json.dumps(ev, default=str), "error": error[:500]}
            await self._client.xadd(self._stream, payload, maxlen=self._maxlen, approximate=True)
        logger.warning("dlq republished %d events to %s: %s", len(events), self._stream, error[:200])
