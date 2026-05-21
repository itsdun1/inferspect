"""Session service test — interleave + sort by started_at."""

from __future__ import annotations

from datetime import datetime

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
