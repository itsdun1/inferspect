"""Tools service test — verifies error_rate calculation + field mapping."""

from __future__ import annotations

from insights_api.services import tools_service

from tests.conftest import FakeCHClient


async def test_tools_compute_error_rate_and_pass_through_fields():
    rows = [
        {"tool_name": "search", "call_count": 100, "p50_latency": 50.0,
         "p95_latency": 200.0, "error_count": 4, "total_bytes": 1024},
        {"tool_name": "calc",   "call_count": 0,   "p50_latency": 0.0,
         "p95_latency": 0.0,   "error_count": 0,  "total_bytes": 0},
    ]
    client = FakeCHClient([rows])

    res = await tools_service.tools(client, window="1h")

    assert res["window"] == "1h"
    assert len(res["tools"]) == 2
    search = res["tools"][0]
    assert search["tool_name"] == "search"
    assert search["call_count"] == 100
    assert search["error_rate"] == 0.04
    assert search["p95_latency_ms"] == 200.0

    # Zero-call tool: error_rate should be 0, not a ZeroDivisionError.
    calc = res["tools"][1]
    assert calc["error_rate"] == 0.0
