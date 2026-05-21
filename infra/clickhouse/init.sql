-- ClickHouse schema for the Ollive logging & ingestion platform.
-- Initialized by the official clickhouse-server entrypoint on first start.

CREATE DATABASE IF NOT EXISTS ollive;
USE ollive;

-- ─────────────────────────────────────────────────────────────────────
-- inference_logs
-- One row per LLM call. ReplacingMergeTree keyed on request_id so retried
-- batches from consumers don't duplicate.
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS inference_logs (
    request_id              UUID,
    conversation_id         UUID,
    session_id              UUID,
    user_id                 Nullable(UUID),
    service                 LowCardinality(String),
    provider                LowCardinality(String),
    model                   LowCardinality(String),
    started_at              DateTime64(3, 'UTC'),
    finished_at             DateTime64(3, 'UTC'),
    received_at             DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC'),
    latency_ms              UInt32,
    ttft_ms                 Nullable(UInt32),
    stream                  UInt8,
    prompt_tokens           UInt32 DEFAULT 0,
    completion_tokens       UInt32 DEFAULT 0,
    total_tokens            UInt32 DEFAULT 0,
    cost_usd                Float64 DEFAULT 0,
    status                  LowCardinality(String),
    finish_reason           LowCardinality(String) DEFAULT '',
    tool_calls_count        UInt16 DEFAULT 0,
    tool_calls_summary      String DEFAULT '[]',   -- JSON array as string
    error_code              Nullable(String),
    error_message           Nullable(String),
    input_preview           String,
    output_preview          String,
    metadata                String DEFAULT '{}'    -- JSON as string for portability
)
ENGINE = ReplacingMergeTree(received_at)
PARTITION BY toYYYYMM(started_at)
ORDER BY (started_at, provider, model, request_id)
TTL toDateTime(started_at) + INTERVAL 90 DAY;

-- ─────────────────────────────────────────────────────────────────────
-- tool_executions
-- One row per tool/function dispatch.
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tool_executions (
    request_id                      UUID,
    tool_call_id                    UUID,
    parent_inference_request_id     UUID,
    conversation_id                 UUID,
    session_id                      UUID,
    user_id                         Nullable(UUID),
    service                         LowCardinality(String),
    tool_name                       LowCardinality(String),
    started_at                      DateTime64(3, 'UTC'),
    finished_at                     DateTime64(3, 'UTC'),
    received_at                     DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC'),
    latency_ms                      UInt32,
    status                          LowCardinality(String),
    error_code                      Nullable(String),
    error_message                   Nullable(String),
    args_preview                    String,
    result_preview                  String,
    result_size_bytes               UInt32 DEFAULT 0,
    metadata                        String DEFAULT '{}'
)
ENGINE = ReplacingMergeTree(received_at)
PARTITION BY toYYYYMM(started_at)
ORDER BY (started_at, tool_name, request_id)
TTL toDateTime(started_at) + INTERVAL 90 DAY;

-- ─────────────────────────────────────────────────────────────────────
-- application_logs
-- Structured application logs (FastAPI services, workers).
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS application_logs (
    ts                      DateTime64(3, 'UTC'),
    received_at             DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC'),
    level                   LowCardinality(String),
    service                 LowCardinality(String),
    logger                  LowCardinality(String) DEFAULT '',
    trace_id                Nullable(UUID),
    span_id                 Nullable(String),
    message                 String,
    attributes              String DEFAULT '{}'
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (ts, service, level)
TTL toDateTime(ts) + INTERVAL 30 DAY;

-- ─────────────────────────────────────────────────────────────────────
-- Materialized views — pre-aggregated rollups for dashboards.
-- ─────────────────────────────────────────────────────────────────────

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_inference_5m
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(bucket)
ORDER BY (bucket, provider, model)
AS SELECT
    toStartOfFiveMinute(started_at) AS bucket,
    provider,
    model,
    quantileState(0.5)(latency_ms)   AS p50_latency_state,
    quantileState(0.95)(latency_ms)  AS p95_latency_state,
    quantileState(0.99)(latency_ms)  AS p99_latency_state,
    quantileState(0.95)(ttft_ms)     AS p95_ttft_state,
    countState()                     AS req_count_state,
    sumState(total_tokens)           AS tokens_state,
    sumState(prompt_tokens)          AS prompt_tokens_state,
    sumState(completion_tokens)      AS completion_tokens_state,
    sumState(cost_usd)               AS cost_state,
    countStateIf(status = 'error')   AS error_count_state,
    countStateIf(status = 'cancelled') AS cancelled_count_state,
    sumState(toUInt64(tool_calls_count)) AS tool_calls_state
FROM inference_logs
GROUP BY bucket, provider, model;

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_tool_5m
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(bucket)
ORDER BY (bucket, tool_name)
AS SELECT
    toStartOfFiveMinute(started_at) AS bucket,
    tool_name,
    countState()                     AS call_count_state,
    quantileState(0.5)(latency_ms)   AS p50_latency_state,
    quantileState(0.95)(latency_ms)  AS p95_latency_state,
    countStateIf(status = 'error')   AS error_count_state,
    sumState(toUInt64(result_size_bytes)) AS bytes_state
FROM tool_executions
GROUP BY bucket, tool_name;
