"""Unit tests for the Phase G control plane.

The ControlService is a thin wrapper over a Valkey LIST + cursor. We swap in
an in-memory fake for the queue here so the test runs without Valkey.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from chatbot_sdk.schema import LogType
from ingestion_service.services.control_service import ControlService
from ingestion_service.services.ingest_service import IngestService
from ingestion_service.services.pii_service import PIIService


# ─── Fakes for ControlQueue ───────────────────────────────────────────


class _FakeControlQueue:
    """Mirror of ControlQueue but backed by an in-memory list per host."""

    def __init__(self) -> None:
        self._queues: dict[str, list[dict]] = {}
        self._cursors: dict[str, int] = {}
        self._heartbeats: dict[str, dict] = {}

    async def enqueue(self, host_id: str, command: dict) -> str:
        self._queues.setdefault(host_id, []).insert(0, command)
        self._cursors[host_id] = self._cursors.get(host_id, 0) + 1
        return str(self._cursors[host_id])

    async def await_commands(self, host_id: str, *, timeout_s: int = 60, max_batch: int = 32):
        # Synchronous fake: drain whatever's there, no real blocking.
        queue = self._queues.get(host_id, [])
        if not queue:
            return [], str(self._cursors.get(host_id, 0))
        commands = []
        while queue and len(commands) < max_batch:
            commands.append(queue.pop())
        return commands, str(self._cursors.get(host_id, 0))

    async def touch_heartbeat(self, host_id: str, *, metadata=None):
        blob = dict(metadata or {})
        blob["last_seen"] = datetime.now(UTC).isoformat()
        self._heartbeats[host_id] = blob

    async def get_heartbeat(self, host_id: str):
        return self._heartbeats.get(host_id)

    async def list_hosts(self):
        return list(self._heartbeats.keys())


# ─── Tests ────────────────────────────────────────────────────────────


async def test_kill_fingerprint_enqueues_command():
    queue = _FakeControlQueue()
    svc = ControlService(queue=queue)

    fp = "a" * 64
    result = await svc.kill_fingerprint(
        host_id="host-1",
        fingerprint=fp,
        reason="operator_kill",
        client="alice-corp",
    )
    assert result["fingerprint"] == fp
    assert result["host_id"] == "host-1"
    # Queue holds the enqueued command.
    cmds, cursor = await queue.await_commands("host-1")
    assert len(cmds) == 1
    assert cmds[0]["command"] == "block_fingerprint"
    assert cmds[0]["fingerprint"] == fp
    assert cmds[0]["reason"] == "operator_kill"
    assert int(cursor) >= 1


async def test_kill_fingerprint_validates_length():
    svc = ControlService(queue=_FakeControlQueue())
    with pytest.raises(ValueError):
        await svc.kill_fingerprint(
            host_id="host-1",
            fingerprint="too-short",
            reason="x",
            client="alice-corp",
        )


async def test_await_commands_returns_empty_on_no_events():
    queue = _FakeControlQueue()
    svc = ControlService(queue=queue)
    result = await svc.await_commands("host-1", timeout_s=1)
    assert result["commands"] == []
    assert "cursor" in result


# ─── Backwards-compat: agent-shaped event flows through ingest cleanly ─


class _FakePublisher:
    def __init__(self):
        self.published: list[tuple[LogType, dict]] = []

    async def publish(self, log_type, event):
        self.published.append((log_type, event))
        return "0-1"


class _FakeIdempotency:
    def __init__(self):
        self.seen: set[str] = set()

    async def mark_or_check(self, request_id: str) -> bool:
        if request_id in self.seen:
            return False
        self.seen.add(request_id)
        return True


async def test_agent_origin_fields_flow_through_ingest():
    pii = PIIService(enabled=False, entities=[])
    svc = IngestService(
        publisher=_FakePublisher(),
        idempotency=_FakeIdempotency(),
        pii=pii,
    )
    payload = {
        "schema_version": "1.0",
        "log_type": "inference",
        "service": "ebpf-agent",
        "request_id": str(uuid4()),
        "provider": "openai",
        "model": "gpt-4o-mini",
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "latency_ms": 0,
        "status": "ok",
        "source": "ebpf-agent",
        "host_id": "my-host-id",
        "process_id": 1234,
        "container_id": "abc123",
        "fingerprint": "f" * 64,
    }
    res = await svc.ingest_batch(
        client="alice-corp",
        service="ebpf-agent",
        sdk_version="ebpf-agent-0.1.0",
        events=[payload],
        received_at=datetime.now(UTC).isoformat(),
    )
    assert res.accepted == 1
    assert res.rejected == 0
    # The published event should carry the agent-origin fields.
    publisher = svc._publisher  # noqa: SLF001 — internal access in test
    _, event = publisher.published[0]
    assert event["source"] == "ebpf-agent"
    assert event["host_id"] == "my-host-id"
    assert event["process_id"] == 1234
    assert event["container_id"] == "abc123"
    assert event["fingerprint"] == "f" * 64
