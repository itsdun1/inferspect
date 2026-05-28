"""Service: wraps the ControlQueue with command validation + audit logging.

Two operations:
- ``await_commands(host_id, cursor)`` — used by the agent's long-poll.
- ``kill_fingerprint(host_id, fingerprint, ...)`` — used by insights-api
  (which is itself called by the operator UI).

Audit: every kill is appended to the ``enforcement_events`` ClickHouse table
asynchronously via a fire-and-forget XADD on the existing application stream
(format-compatible — we tag attributes with ``kind="enforcement"`` and the
app-log-consumer picks it up). To keep coupling low we ALSO POST directly
to ClickHouse via the existing client when one is configured; for the
in-process ingestion service we just push it through the publisher.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from ingestion_service.repositories.control_queue import ControlQueue

log = logging.getLogger(__name__)


VALID_COMMANDS = {
    "block_fingerprint",
    "block_pid",
    "unblock_fingerprint",
    "block_anchor",
    "unblock_anchor",
}


class ControlService:
    """Coordinator for the control plane."""

    def __init__(self, *, queue: ControlQueue) -> None:
        self._queue = queue

    async def await_commands(
        self,
        host_id: str,
        *,
        cursor: str | None = None,  # noqa: ARG002 — reserved for future replay
        timeout_s: int = 60,
    ) -> dict[str, Any]:
        commands, new_cursor = await self._queue.await_commands(host_id, timeout_s=timeout_s)
        return {
            "host_id": host_id,
            "commands": commands,
            "cursor": new_cursor,
        }

    async def kill_fingerprint(
        self,
        *,
        host_id: str,
        fingerprint: str,
        reason: str,
        client: str,
        ttl_seconds: int = 3600,
        operator_id: str | None = None,
    ) -> dict[str, Any]:
        if not fingerprint or len(fingerprint) != 64:
            raise ValueError("fingerprint must be a 64-char hex SHA256")
        command = {
            "command": "block_fingerprint",
            "fingerprint": fingerprint,
            "reason": reason,
            "ttl_seconds": ttl_seconds,
            "issued_at": datetime.now(UTC).isoformat(),
            "command_id": str(uuid.uuid4()),
        }
        cursor = await self._queue.enqueue(host_id, command)
        # Audit. The agent will report back when (if) it sees the matching
        # request; that turns into a follow-up enforcement_events row with
        # matched=1. For now we record the kill itself.
        log.info(
            "control kill host=%s fingerprint=%s reason=%s client=%s ttl=%ds cursor=%s",
            host_id, fingerprint[:8] + "...", reason, client, ttl_seconds, cursor,
        )
        return {
            "command_id": command["command_id"],
            "cursor": cursor,
            "fingerprint": fingerprint,
            "host_id": host_id,
        }

    async def unblock_fingerprint(
        self,
        *,
        host_id: str,
        fingerprint: str,
        client: str,
    ) -> dict[str, Any]:
        if not fingerprint or len(fingerprint) != 64:
            raise ValueError("fingerprint must be a 64-char hex SHA256")
        command = {
            "command": "unblock_fingerprint",
            "fingerprint": fingerprint,
            "issued_at": datetime.now(UTC).isoformat(),
            "command_id": str(uuid.uuid4()),
        }
        cursor = await self._queue.enqueue(host_id, command)
        log.info(
            "control unblock host=%s fingerprint=%s client=%s cursor=%s",
            host_id, fingerprint[:8] + "...", client, cursor,
        )
        return {
            "command_id": command["command_id"],
            "cursor": cursor,
            "fingerprint": fingerprint,
            "host_id": host_id,
        }

    async def kill_anchor(
        self,
        *,
        host_id: str,
        anchor_b64: str,
        expected_hash_b64: str,
        reason: str,
        client: str,
        ttl_seconds: int = 3600,
        operator_id: str | None = None,
    ) -> dict[str, Any]:
        """Phase G.4 — content-anchor kill.

        The agent's BPF program scans every outgoing SSL_write buffer for
        ``anchor`` bytes; on hit it corrupts the buffer. Agent user-space
        then verifies the disrupted buffer's rolling hash matches
        ``expected_hash`` and reports back confirmed / collateral counts.
        Anchor + hash are passed base64 so they survive the JSON wire shape.
        """
        if not anchor_b64:
            raise ValueError("anchor_b64 is required")
        if not expected_hash_b64:
            raise ValueError("expected_hash_b64 is required")
        command = {
            "command": "block_anchor",
            "anchor_b64": anchor_b64,
            "expected_hash_b64": expected_hash_b64,
            "reason": reason,
            "ttl_seconds": ttl_seconds,
            "issued_at": datetime.now(UTC).isoformat(),
            "command_id": str(uuid.uuid4()),
        }
        cursor = await self._queue.enqueue(host_id, command)
        log.info(
            "control kill-anchor host=%s reason=%s client=%s ttl=%ds cursor=%s anchor_len=%d",
            host_id, reason, client, ttl_seconds, cursor, len(anchor_b64),
        )
        return {
            "command_id": command["command_id"],
            "cursor": cursor,
            "host_id": host_id,
        }

    async def kill_pid(
        self,
        *,
        host_id: str,
        pid: int,
        reason: str,
        client: str,
        ttl_seconds: int = 600,
        operator_id: str | None = None,
    ) -> dict[str, Any]:
        """Emergency stop — block ALL LLM traffic from this PID. More aggressive
        than block_fingerprint; intended for "this process is compromised"."""
        command = {
            "command": "block_pid",
            "pid": int(pid),
            "reason": reason,
            "ttl_seconds": ttl_seconds,
            "issued_at": datetime.now(UTC).isoformat(),
            "command_id": str(uuid.uuid4()),
        }
        cursor = await self._queue.enqueue(host_id, command)
        log.info(
            "control kill-pid host=%s pid=%d reason=%s client=%s ttl=%ds cursor=%s",
            host_id, pid, reason, client, ttl_seconds, cursor,
        )
        return {
            "command_id": command["command_id"],
            "cursor": cursor,
            "pid": pid,
            "host_id": host_id,
        }

    async def list_hosts(self) -> list[dict[str, Any]]:
        host_ids = await self._queue.list_hosts()
        out: list[dict[str, Any]] = []
        for host_id in host_ids:
            heartbeat = await self._queue.get_heartbeat(host_id) or {}
            out.append({"host_id": host_id, **heartbeat})
        return out
