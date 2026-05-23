"""Service-level tests for metrics_service. Uses FakeCHClient — no real CH."""

from __future__ import annotations

import pytest
from insights_api.services import metrics_service

from tests.conftest import FakeCHClient


async def test_latency_returns_buckets_and_validates_group():
    rows = [
        {"bucket": "2026-05-21T10:00:00", "provider": "google", "model": "gemini-2.5-pro",
         "p50": 100.0, "p95": 250.0, "p99": 500.0, "req_count": 42},
    ]
    client = FakeCHClient([rows])

    res = await metrics_service.latency(client, window="1h", group="model")

    assert res["window"] == "1h"
    assert res["group"] == "model"
    assert res["buckets"] == rows
    # Verify the repo was called with a parameterized query (no string interpolation
    # of user input). Just check ``since`` is a datetime.
    assert "since" in client.calls[0][1]


async def test_latency_rejects_invalid_group():
    client = FakeCHClient()
    with pytest.raises(ValueError):
        await metrics_service.latency(client, window="1h", group="not-a-thing")


async def test_throughput_computes_per_minute_rates():
    rows = [{"bucket": "2026-05-21T10:00:00", "req_count": 100, "tokens": 5000,
             "prompt_tokens": 2000, "completion_tokens": 3000}]
    client = FakeCHClient([rows])

    res = await metrics_service.throughput(client, window="1h", group="none")

    assert res["buckets"][0]["req_per_min"] == 20.0       # 100 / 5
    assert res["buckets"][0]["tokens_per_min"] == 1000.0  # 5000 / 5


async def test_errors_returns_grouped_counts():
    rows = [{"error_code": "rate_limit", "provider": "google", "error_count": 7,
             "samples": ["x", "y"]}]
    client = FakeCHClient([rows])

    res = await metrics_service.errors(client, window="24h", sample_size=5)

    assert res["window"] == "24h"
    assert res["groups"][0]["error_code"] == "rate_limit"


async def test_cost_returns_by_group_and_top_conversations():
    by_group = [{"provider": "google", "model": "gemini-2.5-pro",
                 "cost_usd": 12.34, "req_count": 50, "tokens": 1000}]
    top = [{"conversation_id": "c1", "cost_usd": 3.21, "tokens": 200, "req_count": 5}]
    client = FakeCHClient([by_group, top])

    res = await metrics_service.cost(client, window="7d", group="model")

    assert res["by_group"] == by_group
    assert res["top_conversations"] == top


async def test_top_conversations_validates_metric_allow_list():
    client = FakeCHClient()
    with pytest.raises(ValueError):
        await metrics_service.top_conversations(
            client, metric="not-real", limit=10, window="1h"
        )


async def test_top_conversations_returns_rows():
    rows = [{"conversation_id": "c1", "session_id": "s1", "provider": "google",
             "model": "gemini-2.5-pro", "cost_usd": 1.0, "tokens": 10,
             "avg_latency_ms": 200.0, "max_latency_ms": 500.0, "req_count": 3,
             "metric_value": 1.0}]
    client = FakeCHClient([rows])

    res = await metrics_service.top_conversations(
        client, metric="cost", limit=10, window="1h"
    )
    assert res["conversations"] == rows
    assert res["metric"] == "cost"


async def test_summary_computes_error_rate_safely_when_zero_requests():
    rows = [{"total_requests": 0, "total_tokens": 0, "total_cost_usd": 0.0,
             "total_errors": 0, "p50_latency": 0, "p95_latency": 0}]
    client = FakeCHClient([rows])

    res = await metrics_service.summary(client, window="1h")
    assert res["error_rate"] == 0.0


async def test_summary_computes_error_rate_when_requests_present():
    rows = [{"total_requests": 100, "total_tokens": 1000, "total_cost_usd": 0.5,
             "total_errors": 4, "p50_latency": 120, "p95_latency": 350}]
    client = FakeCHClient([rows])

    res = await metrics_service.summary(client, window="1h")
    assert res["error_rate"] == pytest.approx(0.04)
    assert res["total_requests"] == 100
    assert res["p95_latency"] == 350.0


async def test_client_filter_appears_in_sql_when_provided():
    """Filtering by client should add an extra WHERE clause + parameter.

    No filter → no ``client`` predicate, no ``client`` parameter.
    With filter → SQL contains ``client = {client:String}`` AND the value
    is parameterized (not interpolated into the string).
    """
    rows = [{"bucket": "2026-05-21T10:00:00", "req_count": 1, "tokens": 1,
             "prompt_tokens": 1, "completion_tokens": 0}]

    # Aggregate (no filter).
    agg_client = FakeCHClient([rows])
    await metrics_service.throughput(agg_client, window="1h", group="none")
    agg_sql, agg_params = agg_client.calls[0]
    assert "client = {client:String}" not in agg_sql
    assert "client" not in agg_params

    # Scoped (with filter).
    scoped_client = FakeCHClient([rows])
    await metrics_service.throughput(
        scoped_client, window="1h", group="none", client="acme"
    )
    scoped_sql, scoped_params = scoped_client.calls[0]
    assert "client = {client:String}" in scoped_sql
    assert scoped_params["client"] == "acme"


async def test_client_filter_threads_through_summary():
    rows = [{"total_requests": 0, "total_tokens": 0, "total_cost_usd": 0.0,
             "total_errors": 0, "p50_latency": 0, "p95_latency": 0}]
    client = FakeCHClient([rows])

    await metrics_service.summary(client, window="1h", client="acme")
    sql, params = client.calls[0]
    assert "client = {client:String}" in sql
    assert params["client"] == "acme"
