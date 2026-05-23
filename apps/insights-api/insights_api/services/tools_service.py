"""Per-tool metrics service — p50/p95 latency, call count, error rate."""

from __future__ import annotations

from typing import Any

from insights_api.repositories import clickhouse_repo as repo
from insights_api.services.window import since_for


async def tools(
    ch_client: Any, *, window: str, client: str | None = None
) -> dict[str, Any]:
    since = since_for(window)
    rows = await repo.tool_metrics(ch_client, since=since, client=client)

    enriched: list[dict[str, Any]] = []
    for row in rows:
        call_count = int(row.get("call_count") or 0)
        error_count = int(row.get("error_count") or 0)
        error_rate = (error_count / call_count) if call_count else 0.0
        enriched.append(
            {
                "tool_name": row["tool_name"],
                "call_count": call_count,
                "error_count": error_count,
                "error_rate": error_rate,
                "p50_latency_ms": float(row.get("p50_latency") or 0.0),
                "p95_latency_ms": float(row.get("p95_latency") or 0.0),
                "total_bytes": int(row.get("total_bytes") or 0),
            }
        )

    return {"window": window, "tools": enriched}
