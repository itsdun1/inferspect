"""Metrics service — latency, throughput, errors, cost, summary, top-conversations.

Stateless: each method takes the ClickHouse client as an argument and delegates
the query to the repository layer. Validation of user-controlled enum values
(``group``, ``metric``) happens here, *before* the repository sees them.

Each method also accepts an optional ``client: str | None`` tenant filter
that is threaded through to the repository.
"""

from __future__ import annotations

from typing import Any

from insights_api.repositories import clickhouse_repo as repo
from insights_api.services.window import since_for

_ALLOWED_GROUPS: frozenset[str] = frozenset({"model", "provider", "none"})
_ALLOWED_METRICS: frozenset[str] = frozenset({"latency", "tokens", "cost"})


def _validate_group(group: str) -> str:
    if group not in _ALLOWED_GROUPS:
        raise ValueError(
            f"invalid group {group!r}; allowed: {sorted(_ALLOWED_GROUPS)}"
        )
    return group


def _validate_metric(metric: str) -> str:
    if metric not in _ALLOWED_METRICS:
        raise ValueError(
            f"invalid metric {metric!r}; allowed: {sorted(_ALLOWED_METRICS)}"
        )
    return metric


async def latency(
    ch_client: Any, *, window: str, group: str, client: str | None = None
) -> dict[str, Any]:
    group = _validate_group(group)
    since = since_for(window)
    rows = await repo.latency_buckets(ch_client, since=since, group=group, client=client)
    return {"window": window, "group": group, "buckets": rows}


async def throughput(
    ch_client: Any, *, window: str, group: str, client: str | None = None
) -> dict[str, Any]:
    group = _validate_group(group)
    since = since_for(window)
    rows = await repo.throughput_buckets(ch_client, since=since, group=group, client=client)

    # Compute per-minute rates (5-min buckets → /5).
    for row in rows:
        req = row.get("req_count") or 0
        tok = row.get("tokens") or 0
        row["req_per_min"] = req / 5.0
        row["tokens_per_min"] = tok / 5.0

    return {"window": window, "group": group, "buckets": rows}


async def errors(
    ch_client: Any, *, window: str, sample_size: int = 5, client: str | None = None
) -> dict[str, Any]:
    since = since_for(window)
    rows = await repo.error_counts(ch_client, since=since, sample_size=sample_size, client=client)
    return {"window": window, "groups": rows}


async def cost(
    ch_client: Any, *, window: str, group: str, client: str | None = None
) -> dict[str, Any]:
    group = _validate_group(group)
    since = since_for(window)
    by_group = await repo.cost_by_group(ch_client, since=since, group=group, client=client)
    top_convos = await repo.top_cost_conversations(ch_client, since=since, limit=10, client=client)
    return {
        "window": window,
        "group": group,
        "by_group": by_group,
        "top_conversations": top_convos,
    }


async def top_conversations(
    ch_client: Any, *, metric: str, limit: int, window: str = "24h", client: str | None = None
) -> dict[str, Any]:
    metric = _validate_metric(metric)
    if limit <= 0 or limit > 200:
        raise ValueError("limit must be 1..200")
    since = since_for(window)
    rows = await repo.top_conversations(
        ch_client, since=since, metric=metric, limit=limit, client=client
    )
    return {"window": window, "metric": metric, "limit": limit, "conversations": rows}


async def summary(ch_client: Any, *, window: str, client: str | None = None) -> dict[str, Any]:
    """Return the rollup the UI's health badge consumes.

    Error rate is derived from (errors / requests). We compute it in Python
    rather than in SQL so we can safely handle the divide-by-zero case.
    """
    since = since_for(window)
    row = await repo.summary_rollup(ch_client, since=since, client=client)
    total_requests = int(row.get("total_requests") or 0)
    total_errors = int(row.get("total_errors") or 0)
    error_rate = (total_errors / total_requests) if total_requests else 0.0
    return {
        "window": window,
        "total_requests": total_requests,
        "total_tokens": int(row.get("total_tokens") or 0),
        "total_cost_usd": float(row.get("total_cost_usd") or 0.0),
        "error_rate": error_rate,
        "p50_latency": float(row.get("p50_latency") or 0.0),
        "p95_latency": float(row.get("p95_latency") or 0.0),
    }
