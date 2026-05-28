"""Repository: per-host outbox for kill/control commands.

Each host gets its own Valkey LIST at ``control:queue:{host_id}``. Commands
are LPUSH'd onto the head; the agent's long-poll consumer does a BLPOP with
a 60s timeout from the tail. When the BLPOP returns, the agent immediately
re-issues another GET — so there's at most one in-flight long-poll per host
at any time.

The cursor returned to the agent is an opaque string we treat as a monotonic
counter. We track it in Valkey at ``control:cursor:{host_id}``. The agent
echoes the last seen cursor on the next poll, but the queue itself is
authoritative — we don't replay past commands on cursor mismatch (the agent
already applied them to its BPF maps).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from redis.asyncio import Redis

log = logging.getLogger(__name__)


class ControlQueue:
    """Per-host command queue backed by a Valkey LIST."""

    def __init__(self, client: Redis, *, ttl_seconds: int = 86400) -> None:
        self._client = client
        # Drop a queue if no command is enqueued for a day — the agent will
        # reconnect and start over.
        self._ttl = ttl_seconds

    @staticmethod
    def _queue_key(host_id: str) -> str:
        return f"control:queue:{host_id}"

    @staticmethod
    def _cursor_key(host_id: str) -> str:
        return f"control:cursor:{host_id}"

    @staticmethod
    def _heartbeat_key(host_id: str) -> str:
        return f"control:heartbeat:{host_id}"

    async def enqueue(self, host_id: str, command: dict[str, Any]) -> str:
        """LPUSH a single command. Returns the new cursor."""
        payload = json.dumps(command, default=str)
        key = self._queue_key(host_id)
        cursor_key = self._cursor_key(host_id)
        pipe = self._client.pipeline()
        pipe.lpush(key, payload)
        pipe.expire(key, self._ttl)
        pipe.incr(cursor_key)
        pipe.expire(cursor_key, self._ttl)
        results = await pipe.execute()
        cursor = str(results[2])
        log.info("control queue enqueue host=%s cursor=%s cmd=%s", host_id, cursor, command.get("command"))
        return cursor

    async def await_commands(
        self,
        host_id: str,
        *,
        timeout_s: int = 60,
        max_batch: int = 32,
    ) -> tuple[list[dict[str, Any]], str]:
        """Block up to ``timeout_s`` seconds waiting for at least one command.

        Returns ``(commands, cursor)``. On timeout returns ``([], cursor)`` so
        the agent can reissue with the same cursor.
        """
        key = self._queue_key(host_id)
        cursor_key = self._cursor_key(host_id)

        # Bump the heartbeat so the registry can show the host as live.
        await self.touch_heartbeat(host_id)

        # BRPOP blocks for up to ``timeout_s``. After the first one returns,
        # we drain anything else already in the queue (non-blocking) so the
        # agent can apply commands in a tight loop.
        first = await self._client.brpop([key], timeout=timeout_s)
        if first is None:
            # Timeout. Return empty + current cursor.
            cursor_raw = await self._client.get(cursor_key)
            return [], cursor_raw or "0"

        commands: list[dict[str, Any]] = [_decode(first[1])]
        # Drain up to max_batch-1 more without blocking.
        for _ in range(max_batch - 1):
            more = await self._client.rpop(key)
            if more is None:
                break
            commands.append(_decode(more))

        cursor_raw = await self._client.get(cursor_key)
        return commands, cursor_raw or "0"

    async def touch_heartbeat(self, host_id: str, *, metadata: dict[str, Any] | None = None) -> None:
        """Record that the agent is alive. Stored as a JSON blob with last_seen."""
        from datetime import datetime, timezone

        blob: dict[str, Any] = metadata.copy() if metadata else {}
        blob["last_seen"] = datetime.now(timezone.utc).isoformat()
        await self._client.set(self._heartbeat_key(host_id), json.dumps(blob), ex=self._ttl)

    async def get_heartbeat(self, host_id: str) -> dict[str, Any] | None:
        raw = await self._client.get(self._heartbeat_key(host_id))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def list_hosts(self) -> list[str]:
        """Return host_ids that have ever heartbeated (heartbeat key still alive)."""
        # SCAN keeps memory bounded. Cluster-safe.
        prefix = "control:heartbeat:"
        out: list[str] = []
        async for key in self._client.scan_iter(match=f"{prefix}*"):
            if isinstance(key, bytes):
                key = key.decode()
            out.append(key[len(prefix):])
        return out


def _decode(raw: Any) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except (TypeError, json.JSONDecodeError):
        pass
    return {"command": "unknown", "raw": str(raw)}
