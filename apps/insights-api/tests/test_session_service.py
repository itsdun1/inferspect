"""Session service test — interleave + sort by started_at."""

from __future__ import annotations

from datetime import datetime

from insights_api.repositories import clickhouse_repo as repo
from insights_api.services import session_service

from tests.conftest import FakeCHClient


async def test_timeline_interleaves_and_sorts():
    inf_rows = [
        {"request_id": "i1", "started_at": datetime(2026, 5, 21, 10, 0, 0), "latency_ms": 100, "session_id": "s1"},
        {"request_id": "i2", "started_at": datetime(2026, 5, 21, 10, 0, 5), "latency_ms": 200, "session_id": "s1"},
    ]
    tool_rows = [
        {"request_id": "t1", "started_at": datetime(2026, 5, 21, 10, 0, 1), "tool_name": "search", "session_id": "s1"},
        {"request_id": "t2", "started_at": datetime(2026, 5, 21, 10, 0, 4), "tool_name": "calc", "session_id": "s1"},
    ]

    client = FakeCHClient([inf_rows, tool_rows])
    res = await session_service.session_timeline(client, session_id="s1")

    assert res["inference_count"] == 2
    assert res["tool_count"] == 2
    assert len(res["timeline"]) == 4

    starts = [ev["started_at"] for ev in res["timeline"]]
    assert starts == sorted(starts)

    types = [ev["event_type"] for ev in res["timeline"]]
    assert types == ["inference", "tool_execution", "tool_execution", "inference"]


async def test_session_inference_events_cross_links_by_fingerprint():
    """Phase G.2: the repo query must widen the match to include agent rows
    that share a fingerprint with the session's direct rows, even if those
    agent rows have a different conversation_id and NULL session_id."""
    # The repo function only fires one query — the fingerprint subquery is
    # inline as a CTE. So we just script one set of rows back.
    rows = [
        # Chat-service-side SDK row (session_id matches directly).
        {
            "request_id": "sdk-1",
            "conversation_id": "c-sdk",
            "session_id": "11111111-1111-1111-1111-111111111111",
            "fingerprint": "fp-abc",
            "source": "sdk",
            "host_id": "",
        },
        # Agent row — different conversation_id, NULL session_id, but
        # same fingerprint, so the cross-link CTE pulls it in.
        {
            "request_id": "agent-1",
            "conversation_id": "c-agent",
            "session_id": None,
            "fingerprint": "fp-abc",
            "source": "ebpf-agent",
            "host_id": "host-A",
        },
    ]
    client = FakeCHClient([rows])

    result = await repo.session_inference_events(
        client, session_id="11111111-1111-1111-1111-111111111111"
    )

    # Both rows surfaced.
    assert len(result) == 2
    assert {r["request_id"] for r in result} == {"sdk-1", "agent-1"}

    # The query must use the fingerprint-CTE shape, not just direct
    # session_id/conversation_id matching.
    sql, params = client.calls[0]
    assert "session_fingerprints" in sql
    assert "fingerprint IN (SELECT fingerprint FROM session_fingerprints)" in sql
    # New projection columns to support the cross-link UX.
    assert "source" in sql and "host_id" in sql and "fingerprint" in sql
    assert params["id"] == "11111111-1111-1111-1111-111111111111"


async def test_session_inference_events_scopes_by_client_when_provided():
    """The cross-link must still respect the multi-tenant ``client`` filter."""
    client = FakeCHClient([[]])

    await repo.session_inference_events(
        client,
        session_id="11111111-1111-1111-1111-111111111111",
        client="acme",
    )

    sql, params = client.calls[0]
    assert "client = {client:String}" in sql
    assert params["client"] == "acme"
