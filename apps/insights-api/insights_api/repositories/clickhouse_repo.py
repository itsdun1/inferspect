"""Repository: the only layer that touches ClickHouse.

All query strings live here. Each function is async, accepts a client (the
``_AsyncCHClient`` protocol — clickhouse-connect's async client at runtime,
or a fake in tests), and returns raw ``list[dict[str, Any]]``.

User-controlled values are passed via parameterized queries (``parameters=``)
so we never interpolate into SQL strings. The ``group_by`` knob is the one
exception — it's a column name and is validated against a finite allow-list
by the service layer before we get here.

Multi-tenancy: every aggregation accepts an optional ``client`` argument. When
provided, an additional ``client = {client:String}`` predicate is added to
the WHERE clause to scope the query to a single tenant.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol


class _AsyncCHClient(Protocol):
    async def query(
        self,
        query: str,
        parameters: dict[str, Any] | None = ...,
    ) -> Any: ...
    async def close(self) -> None: ...


async def _named_results(client: _AsyncCHClient, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Run a parameterized query and return rows as a list of dicts."""
    result = await client.query(sql, parameters=params)
    # clickhouse-connect QueryResult exposes ``named_results()`` returning an
    # iterable of dict[col, value]. We materialize it for testability.
    return list(result.named_results())


def _client_filter(client: str | None) -> str:
    """Return an extra ``AND client = {client:String}`` clause when scoped."""
    return " AND client = {client:String}" if client is not None else ""


# ─── /insights/latency ────────────────────────────────────────────
async def latency_buckets(
    ch_client: _AsyncCHClient,
    *,
    since: datetime,
    group: str = "model",
    client: str | None = None,
) -> list[dict[str, Any]]:
    """p50/p95/p99 latency per (provider, group) per 5-min bucket from mv_inference_5m."""
    # ``group`` is validated upstream (see metrics_service); allow-list is
    # {"model", "provider", "none"}. Safe to embed.
    group_cols = _group_columns(group)
    select_group = ", ".join(group_cols) if group_cols else ""
    group_clause = f", {select_group}" if select_group else ""
    group_by_tail = f", {select_group}" if select_group else ""

    sql = f"""
        SELECT
            bucket {group_clause},
            quantileMerge(0.5)(p50_latency_state)  AS p50,
            quantileMerge(0.95)(p95_latency_state) AS p95,
            quantileMerge(0.99)(p99_latency_state) AS p99,
            countMerge(req_count_state)            AS req_count
        FROM mv_inference_5m
        WHERE bucket >= {{since:DateTime64(3)}}{_client_filter(client)}
        GROUP BY bucket {group_by_tail}
        ORDER BY bucket ASC {group_by_tail}
    """
    params: dict[str, Any] = {"since": since}
    if client is not None:
        params["client"] = client
    return await _named_results(ch_client, sql, params)


# ─── /insights/throughput ─────────────────────────────────────────
async def throughput_buckets(
    ch_client: _AsyncCHClient,
    *,
    since: datetime,
    group: str = "none",
    client: str | None = None,
) -> list[dict[str, Any]]:
    """Requests + tokens per 5-min bucket, optionally grouped."""
    group_cols = _group_columns(group)
    select_group = ", ".join(group_cols) if group_cols else ""
    group_clause = f", {select_group}" if select_group else ""
    group_by_tail = f", {select_group}" if select_group else ""

    sql = f"""
        SELECT
            bucket {group_clause},
            countMerge(req_count_state) AS req_count,
            sumMerge(tokens_state)      AS tokens,
            sumMerge(prompt_tokens_state)     AS prompt_tokens,
            sumMerge(completion_tokens_state) AS completion_tokens
        FROM mv_inference_5m
        WHERE bucket >= {{since:DateTime64(3)}}{_client_filter(client)}
        GROUP BY bucket {group_by_tail}
        ORDER BY bucket ASC {group_by_tail}
    """
    params: dict[str, Any] = {"since": since}
    if client is not None:
        params["client"] = client
    return await _named_results(ch_client, sql, params)


# ─── /insights/errors ─────────────────────────────────────────────
async def error_counts(
    ch_client: _AsyncCHClient,
    *,
    since: datetime,
    sample_size: int = 5,
    client: str | None = None,
) -> list[dict[str, Any]]:
    """Error counts grouped by (error_code, provider), with up to N sample messages."""
    sql = f"""
        SELECT
            coalesce(error_code, '') AS error_code,
            provider,
            count() AS error_count,
            arraySlice(groupArray(error_message), 1, {{sample_size:UInt32}}) AS samples
        FROM inference_logs
        WHERE started_at >= {{since:DateTime64(3)}}
          AND status = 'error'{_client_filter(client)}
        GROUP BY error_code, provider
        ORDER BY error_count DESC
    """
    params: dict[str, Any] = {"since": since, "sample_size": sample_size}
    if client is not None:
        params["client"] = client
    return await _named_results(ch_client, sql, params)


# ─── /insights/cost ───────────────────────────────────────────────
async def cost_by_group(
    ch_client: _AsyncCHClient,
    *,
    since: datetime,
    group: str = "model",
    client: str | None = None,
) -> list[dict[str, Any]]:
    """Total spend per (model | provider) over the window."""
    group_cols = _group_columns(group) or ["model"]
    select_group = ", ".join(group_cols)
    sql = f"""
        SELECT
            {select_group},
            sumMerge(cost_state)       AS cost_usd,
            countMerge(req_count_state) AS req_count,
            sumMerge(tokens_state)     AS tokens
        FROM mv_inference_5m
        WHERE bucket >= {{since:DateTime64(3)}}{_client_filter(client)}
        GROUP BY {select_group}
        ORDER BY cost_usd DESC
    """
    params: dict[str, Any] = {"since": since}
    if client is not None:
        params["client"] = client
    return await _named_results(ch_client, sql, params)


async def top_cost_conversations(
    ch_client: _AsyncCHClient,
    *,
    since: datetime,
    limit: int = 10,
    client: str | None = None,
) -> list[dict[str, Any]]:
    sql = f"""
        SELECT
            conversation_id,
            sum(cost_usd)   AS cost_usd,
            sum(total_tokens) AS tokens,
            count()         AS req_count
        FROM inference_logs
        WHERE started_at >= {{since:DateTime64(3)}}{_client_filter(client)}
        GROUP BY conversation_id
        ORDER BY cost_usd DESC
        LIMIT {{limit:UInt32}}
    """
    params: dict[str, Any] = {"since": since, "limit": limit}
    if client is not None:
        params["client"] = client
    return await _named_results(ch_client, sql, params)


# ─── /insights/top-conversations ──────────────────────────────────
async def top_conversations(
    ch_client: _AsyncCHClient,
    *,
    since: datetime,
    metric: str,
    limit: int = 20,
    client: str | None = None,
) -> list[dict[str, Any]]:
    """Outlier conversations sorted by metric DESC. Caller validates ``metric``."""
    # metric is validated against an allow-list upstream.
    metric_expr = _metric_expr(metric)
    sql = f"""
        SELECT
            conversation_id,
            session_id,
            any(provider)  AS provider,
            any(model)     AS model,
            sum(cost_usd)  AS cost_usd,
            sum(total_tokens) AS tokens,
            avg(latency_ms) AS avg_latency_ms,
            max(latency_ms) AS max_latency_ms,
            count()        AS req_count,
            {metric_expr}  AS metric_value
        FROM inference_logs
        WHERE started_at >= {{since:DateTime64(3)}}{_client_filter(client)}
        GROUP BY conversation_id, session_id
        ORDER BY metric_value DESC
        LIMIT {{limit:UInt32}}
    """
    params: dict[str, Any] = {"since": since, "limit": limit}
    if client is not None:
        params["client"] = client
    return await _named_results(ch_client, sql, params)


# ─── /insights/sessions/{session_id} ──────────────────────────────
async def session_inference_events(
    ch_client: _AsyncCHClient,
    *,
    session_id: str,
    client: str | None = None,
) -> list[dict[str, Any]]:
    sql = f"""
        SELECT
            request_id,
            conversation_id,
            session_id,
            provider,
            model,
            started_at,
            finished_at,
            latency_ms,
            ttft_ms,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            cost_usd,
            status,
            error_code,
            error_message,
            tool_calls_count,
            input_preview,
            output_preview
        FROM inference_logs
        WHERE (session_id = {{id:UUID}} OR conversation_id = {{id:UUID}}){_client_filter(client)}
        ORDER BY started_at ASC
    """
    params: dict[str, Any] = {"id": session_id}
    if client is not None:
        params["client"] = client
    return await _named_results(ch_client, sql, params)


async def session_tool_events(
    ch_client: _AsyncCHClient,
    *,
    session_id: str,
    client: str | None = None,
) -> list[dict[str, Any]]:
    sql = f"""
        SELECT
            request_id,
            tool_call_id,
            parent_inference_request_id,
            conversation_id,
            session_id,
            tool_name,
            started_at,
            finished_at,
            latency_ms,
            status,
            error_code,
            error_message,
            args_preview,
            result_preview,
            result_size_bytes
        FROM tool_executions
        WHERE (session_id = {{id:UUID}} OR conversation_id = {{id:UUID}}){_client_filter(client)}
        ORDER BY started_at ASC
    """
    params: dict[str, Any] = {"id": session_id}
    if client is not None:
        params["client"] = client
    return await _named_results(ch_client, sql, params)


# ─── /insights/sdk-overhead ───────────────────────────────────────
async def sdk_overhead_quantiles(
    ch_client: _AsyncCHClient,
    *,
    since: datetime,
    client: str | None = None,
) -> dict[str, Any]:
    """Quantiles over metadata.sdk_overhead_ms and metadata.api_call_ms.

    Reads from raw inference_logs (not an MV) — metadata is a JSON column,
    can't be materialized. Only counts rows where the field is present
    (older SDK versions don't emit it)."""
    sql = f"""
        SELECT
            count() AS sample_size,
            quantile(0.5)(toFloat64OrZero(JSONExtractString(metadata, 'sdk_overhead_ms')))  AS p50_ms,
            quantile(0.95)(toFloat64OrZero(JSONExtractString(metadata, 'sdk_overhead_ms'))) AS p95_ms,
            quantile(0.99)(toFloat64OrZero(JSONExtractString(metadata, 'sdk_overhead_ms'))) AS p99_ms,
            max(toFloat64OrZero(JSONExtractString(metadata, 'sdk_overhead_ms')))            AS max_ms,
            quantile(0.5)(toFloat64OrZero(JSONExtractString(metadata, 'api_call_ms')))      AS p50_api_ms,
            quantile(0.95)(toFloat64OrZero(JSONExtractString(metadata, 'api_call_ms')))     AS p95_api_ms
        FROM inference_logs
        WHERE received_at >= {{since:DateTime64(3)}}
          AND JSONHas(metadata, 'sdk_overhead_ms')
          {_client_filter(client)}
    """
    params: dict[str, Any] = {"since": since}
    if client is not None:
        params["client"] = client
    rows = await _named_results(ch_client, sql, params)
    return rows[0] if rows else {}


# ─── /insights/summary ────────────────────────────────────────────
async def summary_rollup(
    ch_client: _AsyncCHClient,
    *,
    since: datetime,
    client: str | None = None,
) -> dict[str, Any]:
    """Single-row rollup of headline numbers over the window."""
    sql = f"""
        SELECT
            countMerge(req_count_state)             AS total_requests,
            sumMerge(tokens_state)                  AS total_tokens,
            sumMerge(cost_state)                    AS total_cost_usd,
            countMerge(error_count_state)           AS total_errors,
            quantileMerge(0.5)(p50_latency_state)   AS p50_latency,
            quantileMerge(0.95)(p95_latency_state)  AS p95_latency
        FROM mv_inference_5m
        WHERE bucket >= {{since:DateTime64(3)}}{_client_filter(client)}
    """
    params: dict[str, Any] = {"since": since}
    if client is not None:
        params["client"] = client
    rows = await _named_results(ch_client, sql, params)
    return rows[0] if rows else {
        "total_requests": 0,
        "total_tokens": 0,
        "total_cost_usd": 0.0,
        "total_errors": 0,
        "p50_latency": 0,
        "p95_latency": 0,
    }


# ─── /insights/anomalies ──────────────────────────────────────────
async def anomaly_series(
    ch_client: _AsyncCHClient,
    *,
    since: datetime,
    client: str | None = None,
) -> list[dict[str, Any]]:
    """Per-(provider, model, bucket) latency + error count series for anomaly detection.

    The service layer computes z-scores on top of this.
    """
    sql = f"""
        SELECT
            bucket,
            provider,
            model,
            quantileMerge(0.95)(p95_latency_state) AS p95_latency,
            countMerge(req_count_state)            AS req_count,
            countMerge(error_count_state)          AS error_count
        FROM mv_inference_5m
        WHERE bucket >= {{since:DateTime64(3)}}{_client_filter(client)}
        GROUP BY bucket, provider, model
        ORDER BY provider, model, bucket ASC
    """
    params: dict[str, Any] = {"since": since}
    if client is not None:
        params["client"] = client
    return await _named_results(ch_client, sql, params)


# ─── /insights/tools ──────────────────────────────────────────────
async def tool_metrics(
    ch_client: _AsyncCHClient,
    *,
    since: datetime,
    client: str | None = None,
) -> list[dict[str, Any]]:
    """Per-tool aggregated metrics from mv_tool_5m + a tool_executions fallback.

    We pull p50/p95 + call/error counts from the materialized view.
    """
    sql = f"""
        SELECT
            tool_name,
            countMerge(call_count_state)           AS call_count,
            quantileMerge(0.5)(p50_latency_state)  AS p50_latency,
            quantileMerge(0.95)(p95_latency_state) AS p95_latency,
            countMerge(error_count_state)          AS error_count,
            sumMerge(bytes_state)                  AS total_bytes
        FROM mv_tool_5m
        WHERE bucket >= {{since:DateTime64(3)}}{_client_filter(client)}
        GROUP BY tool_name
        ORDER BY call_count DESC
    """
    params: dict[str, Any] = {"since": since}
    if client is not None:
        params["client"] = client
    return await _named_results(ch_client, sql, params)


# ─── helpers ──────────────────────────────────────────────────────
_ALLOWED_GROUPS = {"model", "provider", "none"}
_ALLOWED_METRICS = {"latency", "tokens", "cost"}


def _group_columns(group: str) -> list[str]:
    """Translate a validated ``group`` to a list of SQL column names."""
    if group == "model":
        return ["provider", "model"]
    if group == "provider":
        return ["provider"]
    return []  # "none"


def _metric_expr(metric: str) -> str:
    """SQL expression for the ranking metric in top_conversations."""
    if metric == "latency":
        return "avg(latency_ms)"
    if metric == "tokens":
        return "sum(total_tokens)"
    if metric == "cost":
        return "sum(cost_usd)"
    # Unreachable — service layer validates first. Defensive default.
    return "sum(cost_usd)"
