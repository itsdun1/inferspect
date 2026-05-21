"""Repository: idempotency dedup via Valkey ``SET NX``.

Each event's ``request_id`` is checked against a short-lived key. If the key
already exists, the event is a duplicate of a recent retry and we skip it.
"""

from __future__ import annotations

from redis.asyncio import Redis


class IdempotencyRepository:
    def __init__(self, client: Redis, *, ttl_s: int = 600) -> None:
        self._client = client
        self._ttl_s = ttl_s

    async def mark_or_check(self, request_id: str) -> bool:
        """Return True if this is the first time we've seen ``request_id``
        within the TTL window; False if it's a duplicate."""
        key = f"idem:{request_id}"
        # SET NX EX — atomic claim + expire.
        result = await self._client.set(key, "1", nx=True, ex=self._ttl_s)
        return bool(result)
