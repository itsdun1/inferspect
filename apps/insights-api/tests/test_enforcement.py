"""Repo tests for /enforcement-events — verify ``matched`` is computed
dynamically against ``inference_logs.metadata.event = 'kill_applied'``
rather than read from the (never-flipped) stored column.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from insights_api.repositories import clickhouse_repo as repo

from tests.conftest import FakeCHClient


async def test_enforcement_events_computes_matched_via_kill_applied_subquery():
    # The repo returns whatever ClickHouse hands back, so for the unit test
    # we just script rows already labelled with matched=0/1 as ClickHouse
    # would produce after evaluating the inline subquery.
    rows = [
        {
            "timestamp": datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
            "host_id": "host-A",
            "fingerprint": "fp-1",
            "command": "block_fingerprint",
            "reason": "operator_kill",
            "source": "operator",
            "client": "acme",
            "rule_id": "",
            "operator_id": "op-1",
            "matched": 1,
        },
        {
            "timestamp": datetime(2026, 5, 28, 12, 5, 0, tzinfo=UTC),
            "host_id": "host-B",
            "fingerprint": "fp-2",
            "command": "block_fingerprint",
            "reason": "operator_kill",
            "source": "operator",
            "client": "acme",
            "rule_id": "",
            "operator_id": "op-1",
            "matched": 0,
        },
    ]
    client = FakeCHClient([rows])

    since = datetime.now(UTC) - timedelta(hours=1)
    result = await repo.enforcement_events(client, since=since, limit=50)

    assert len(result) == 2
    assert result[0]["matched"] == 1
    assert result[1]["matched"] == 0

    sql, params = client.calls[0]
    # The query must compute matched dynamically — proves we're no longer
    # reading the stored column verbatim.
    assert "kill_applied" in sql
    assert "JSONExtractString(il.metadata, 'event')" in sql
    assert "ee.timestamp + INTERVAL 5 MINUTE" in sql
    # And still parameterizes the time window + limit.
    assert "since" in params
    assert params["limit"] == 50


async def test_enforcement_events_applies_host_and_client_filters():
    client = FakeCHClient([[]])

    since = datetime.now(UTC) - timedelta(hours=24)
    await repo.enforcement_events(
        client,
        since=since,
        host_id="host-A",
        client="acme",
        limit=10,
    )

    sql, params = client.calls[0]
    assert "ee.host_id = {host_id:String}" in sql
    assert "ee.client = {client:String}" in sql
    assert params["host_id"] == "host-A"
    assert params["client"] == "acme"
    assert params["limit"] == 10
