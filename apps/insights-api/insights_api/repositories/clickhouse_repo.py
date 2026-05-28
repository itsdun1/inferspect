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
    """Return inference rows for a session, cross-linked by fingerprint.

    Phase G.2 — since the agent emits rows with NULL ``session_id`` and a
    different ``conversation_id`` than chat-service, we widen the match:
    any inference_logs row that shares a fingerprint with the session's
    direct rows is included. Rows without a fingerprint (the default for
    pre-agent SDK rows that don't compute one) are still matched by
    session_id/conversation_id directly so we don't drop them.
    """
    sql = f"""
        WITH session_fingerprints AS (
            SELECT DISTINCT fingerprint
            FROM inference_logs
            WHERE (session_id = {{id:UUID}} OR conversation_id = {{id:UUID}})
              AND fingerprint != ''
        )
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
            output_preview,
            source,
            host_id,
            fingerprint
        FROM inference_logs
        WHERE (
            session_id = {{id:UUID}}
            OR conversation_id = {{id:UUID}}
            OR (fingerprint != '' AND fingerprint IN (SELECT fingerprint FROM session_fingerprints))
        ){_client_filter(client)}
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


# ─── /agents ──────────────────────────────────────────────────────
async def connected_agents(
    ch_client: _AsyncCHClient,
    *,
    since: datetime,
    client: str | None = None,
) -> list[dict[str, Any]]:
    """Distinct host_ids that emitted at least one ebpf-agent event in the window.

    ClickHouse is the source of truth for who's been heartbeating recently —
    cheaper than reaching across to ingestion's Valkey. The trade-off: a host
    that's connected but hasn't observed traffic yet won't appear here. For
    fleet visibility this is the right behavior — we surface hosts that are
    actually doing observability work.
    """
    sql = f"""
        SELECT
            host_id,
            max(received_at)              AS last_seen,
            count()                       AS event_count,
            any(container_id)             AS container_id,
            uniqExact(process_id)         AS distinct_pids,
            uniqExact(provider)           AS distinct_providers
        FROM inference_logs
        WHERE received_at >= {{since:DateTime64(3)}}
          AND source = 'ebpf-agent'
          AND host_id != ''{_client_filter(client)}
        GROUP BY host_id
        ORDER BY last_seen DESC
    """
    params: dict[str, Any] = {"since": since}
    if client is not None:
        params["client"] = client
    return await _named_results(ch_client, sql, params)


# ─── /agents/{host_id}/fingerprints ───────────────────────────────
async def recent_fingerprints_for_host(
    ch_client: _AsyncCHClient,
    *,
    host_id: str,
    since: datetime,
    limit: int = 20,
    client: str | None = None,
) -> list[dict[str, Any]]:
    """Distinct fingerprints captured on this host in the window, with stats.

    Used by the operator-console agent page so an operator can browse what
    conversations a given host has observed and kill any of them. Each row:
    fingerprint (64-hex), first_seen, last_seen, request_count, sample preview
    of the first user-message-shaped wire body, distinct process_ids.
    """
    sql = f"""
        SELECT
            fingerprint,
            min(received_at)                AS first_seen,
            max(received_at)                AS last_seen,
            count()                         AS request_count,
            any(input_preview)              AS sample_preview,
            any(model)                      AS model,
            any(provider)                   AS provider,
            uniqExact(process_id)           AS distinct_pids
        FROM inference_logs
        WHERE host_id = {{host_id:String}}
          AND source = 'ebpf-agent'
          AND fingerprint != ''
          AND received_at >= {{since:DateTime64(3)}}{_client_filter(client)}
        GROUP BY fingerprint
        ORDER BY last_seen DESC
        LIMIT {{limit:UInt32}}
    """
    params: dict[str, Any] = {"host_id": host_id, "since": since, "limit": limit}
    if client is not None:
        params["client"] = client
    return await _named_results(ch_client, sql, params)


# ─── /enforcement-events ──────────────────────────────────────────
async def enforcement_events(
    ch_client: _AsyncCHClient,
    *,
    since: datetime,
    host_id: str | None = None,
    limit: int = 100,
    client: str | None = None,
) -> list[dict[str, Any]]:
    """Return enforcement audit rows with ``matched`` computed dynamically.

    Phase G.2 — the stored ``matched`` column is never flipped (the agent
    writes its kill_applied confirmation into ``inference_logs``, not back
    into enforcement_events). Compute it at query time: a kill is
    considered matched if any ``kill_applied`` event landed on the same
    host within 5 minutes after the kill was issued. ``host_id``
    correlation is sufficient — fingerprint correlation would also work
    but the agent only fires kill_applied for kills it received, so
    host+time is unambiguous in practice.
    """
    host_filter = " AND ee.host_id = {host_id:String}" if host_id is not None else ""
    client_clause = " AND ee.client = {client:String}" if client is not None else ""
    # ClickHouse 24.8 rejects correlated subqueries that reference
    # non-constant columns from the outer scope (only constants/CTEs
    # allowed). Rewrite the matched-flag as a LEFT JOIN against the kill
    # confirmation events, time-bucketed against each kill.
    sql = f"""
        SELECT
            ee.timestamp        AS timestamp,
            ee.host_id          AS host_id,
            ee.fingerprint      AS fingerprint,
            ee.command          AS command,
            ee.reason           AS reason,
            ee.source           AS source,
            ee.client           AS client,
            ee.rule_id          AS rule_id,
            ee.operator_id      AS operator_id,
            toUInt8(max(if(
                kill.host_id != ''
                AND kill.received_at >= ee.timestamp
                AND kill.received_at <= ee.timestamp + INTERVAL 5 MINUTE,
                1, 0
            ))) AS matched
        FROM enforcement_events ee
        LEFT JOIN (
            SELECT host_id, received_at
            FROM inference_logs
            WHERE JSONExtractString(metadata, 'event') = 'kill_applied'
              AND received_at >= {{since:DateTime64(3)}}
              AND received_at <= now() + INTERVAL 1 HOUR
        ) kill ON kill.host_id = ee.host_id
        WHERE ee.timestamp >= {{since:DateTime64(3)}}{host_filter}{client_clause}
        GROUP BY
            ee.timestamp, ee.host_id, ee.fingerprint, ee.command, ee.reason,
            ee.source, ee.client, ee.rule_id, ee.operator_id
        ORDER BY ee.timestamp DESC
        LIMIT {{limit:UInt32}}
    """
    params: dict[str, Any] = {"since": since, "limit": limit}
    if host_id is not None:
        params["host_id"] = host_id
    if client is not None:
        params["client"] = client
    return await _named_results(ch_client, sql, params)


async def session_fingerprint(
    ch_client: _AsyncCHClient,
    *,
    session_id: str,
    client: str | None = None,
) -> dict[str, Any] | None:
    """Return the host_id + fingerprint + input_preview for the most recent
    ebpf-agent row in this session.

    Used by ``agents_service.kill_session`` — it needs:
      * ``host_id`` to route the kill command,
      * ``fingerprint`` for the audit log,
      * ``input_preview`` so Phase G.4 can extract the anchor bytes + the
        rolling-hash chain for the agent's Layer 2 verifier.

    Just returns the single most recent matching row (no GROUP BY, which
    triggered ClickHouse 24.8's stricter check for column references in
    both WHERE and aggregate SELECT).
    """
    # The agent emits one row per HTTP request *and* a separate row for the
    # response stitch; only the request row has the wire body in
    # ``input_preview``. Filter on ``length(input_preview) > 0`` so we land
    # on a row that carries the JSON we need to extract the anchor from.
    sql = f"""
        SELECT
            host_id,
            fingerprint,
            conversation_id,
            received_at AS last_seen,
            input_preview
        FROM inference_logs
        WHERE (session_id = {{id:UUID}} OR conversation_id = {{id:UUID}})
          AND source = 'ebpf-agent'
          AND fingerprint != ''
          AND length(input_preview) > 0{_client_filter(client)}
        ORDER BY received_at DESC
        LIMIT 1
    """
    params: dict[str, Any] = {"id": session_id}
    if client is not None:
        params["client"] = client
    rows = await _named_results(ch_client, sql, params)
    if rows:
        return rows[0]

    # Fallback path. When the SDK is disabled in the customer's app
    # (Phase G's whole point — daemon-only observation), agent rows DON'T
    # share chat-service's conversation_id (they carry their own
    # tracker-minted AgentID). The direct match above will miss every
    # time. Until we cross-link by fingerprint via chat-service's
    # postgres, fall back to "the most recent ebpf-agent capture in the
    # last 5 minutes". For single-host demos this is exact; for
    # multi-tenant we'll need the postgres cross-link.
    fallback_sql = f"""
        SELECT
            host_id,
            fingerprint,
            conversation_id,
            received_at AS last_seen,
            input_preview
        FROM inference_logs
        WHERE source = 'ebpf-agent'
          AND fingerprint != ''
          AND length(input_preview) > 0
          AND received_at >= now() - INTERVAL 5 MINUTE
          {_client_filter(client)}
        ORDER BY received_at DESC
        LIMIT 1
    """
    # Use a fresh params dict — the first query used {id}, but the fallback
    # only references {client} (when present). Passing extra ``id`` would
    # confuse the parameterized-query type checker.
    fallback_params: dict[str, Any] = {}
    if client is not None:
        fallback_params["client"] = client
    fallback_rows = await _named_results(ch_client, fallback_sql, fallback_params)
    return fallback_rows[0] if fallback_rows else None


async def preview_for_fingerprint(
    ch_client: _AsyncCHClient,
    *,
    host_id: str,
    fingerprint: str,
    client: str | None = None,
) -> str | None:
    """Latest non-empty ``input_preview`` for a (host_id, fingerprint) pair.

    Used by the /agents page Kill button so it can go through the Phase G.4
    anchor flow instead of the older fingerprint-pattern path — the operator
    has already picked a conversation from the agent's own view, no need to
    cross-reference chat-service.
    """
    sql = f"""
        SELECT input_preview
        FROM inference_logs
        WHERE host_id = {{host_id:String}}
          AND fingerprint = {{fingerprint:String}}
          AND source = 'ebpf-agent'
          AND length(input_preview) > 0{_client_filter(client)}
        ORDER BY received_at DESC
        LIMIT 1
    """
    params: dict[str, Any] = {"host_id": host_id, "fingerprint": fingerprint}
    if client is not None:
        params["client"] = client
    rows = await _named_results(ch_client, sql, params)
    if not rows:
        return None
    raw = rows[0].get("input_preview") or ""
    return raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)


async def record_enforcement_event(
    ch_client: _AsyncCHClient,
    *,
    host_id: str,
    fingerprint: str,
    command: str,
    reason: str,
    source: str,
    client_name: str,
    operator_id: str = "",
    rule_id: str = "",
) -> None:
    """Append a row to ``enforcement_events``. Fire-and-forget audit log."""
    # clickhouse-connect's async client also has ``insert()`` like the
    # consumer's writer. Same shape.
    await ch_client.insert(
        "enforcement_events",
        [[host_id, fingerprint, command, reason, source, client_name, rule_id, operator_id, 0]],
        column_names=[
            "host_id",
            "fingerprint",
            "command",
            "reason",
            "source",
            "client",
            "rule_id",
            "operator_id",
            "matched",
        ],
    )


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
