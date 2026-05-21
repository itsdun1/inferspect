# Architecture

Deeper companion to the [README](./README.md). Skim the README first for product positioning and the quick start; this document covers the data plane, the SDK, failure modes, idempotency, scaling envelopes, and the security posture.

## Ingestion flow

```
chat-service                    ingestion-service             FastStream consumer        ClickHouse
┌─────────────────┐  POST       ┌─────────────────┐  XADD     ┌─────────────────┐  bulk  ┌────────────┐
│ LangGraph agent │  /v1/logs   │ schema validate │  stream   │ XREADGROUP      │ insert │ inference_ │
│ + SDK callback  │ ─────────►  │ Presidio PII    │ ────────► │ batch up to 200 │ ─────► │   logs     │
│ (in-process     │  batched    │ Valkey dedup    │           │ → DLQ on fail   │        │            │
│  asyncio queue) │             │ (SET NX)        │           │                 │        │            │
└─────────────────┘             └─────────────────┘           └─────────────────┘        └────────────┘
                                                                                              │
                                                                                              ▼
                                                                                       insights-api
                                                                                       (mv_inference_5m
                                                                                        quantileMerge…)
```

Three log types share this pipeline:

1. **`inference`** — per-LLM-call envelope: provider, model, latency, ttft, tokens, cost, status, finish_reason, `tool_calls_summary`.
2. **`tool_execution`** — per-tool-dispatch envelope: tool_name, args, result, latency, `parent_inference_request_id`.
3. **`application`** — structlog-shipped service logs: ts, level, service, message, attributes.

All three flow through one HTTP endpoint, one broker pattern, and one consumer-group abstraction, but land in three ClickHouse tables with distinct schemas. Discrimination is by `log_type` at every layer.

Step by step for a single inference event:

1. **Capture.** A LangChain callback fires on `on_chat_model_start` / `on_llm_new_token` / `on_chat_model_end`. The SDK opens an `InferenceSpan` keyed by `run_id`, records ttft on the first chunk, accumulates content + token counts, and finalizes on completion. The completed span is submitted to the in-process transport.
2. **Batch.** The transport pushes the envelope onto a bounded deque, signals a background flusher, and returns. The chat path never blocks.
3. **POST.** The flusher accumulates up to 64 events or 1s and POSTs `{"events": [...]}` to `ingestion-service` at `/v1/logs`. Optional `X-Sdk-Key` header authenticates the sender.
4. **Validate.** The ingestion service routes by `log_type` to a versioned Pydantic schema. Unknown types return 422. Older schema versions are accepted through a migration map.
5. **Enrich.** Server-side `received_at` timestamp, ingestion node id, normalized `provider` casing, derived flags (`is_error`, `cost_usd_estimated` when the SDK didn't compute one).
6. **Redact.** Presidio analyzer + anonymizer runs on `input_preview`, `output_preview`, and any string field in `metadata`. Recognizers are config-driven (`PII_RECOGNIZERS` env).
7. **Dedupe.** `SET NX idem:<request_id>` against Valkey with a 10-minute TTL. Duplicates are accepted with `status='duplicate'` and never reach the broker.
8. **Publish.** Event is `XADD`'d to `inference.v1` (or `tool_executions.v1` / `application.v1`) with a `MAXLEN ~` cap. The HTTP response returns 202 with per-event accept/reject status.
9. **Consume.** A FastStream worker with a consumer group `XREADGROUP`s up to 200 events or 1s, batches them, and bulk-inserts into ClickHouse via `clickhouse-connect`'s async client.
10. **Persist.** The `ReplacingMergeTree` engine accepts the batch. On any insert failure, the consumer publishes the batch to a per-stream DLQ (`*.v1.dlq`) and ACKs the original so it doesn't loop forever.
11. **Aggregate.** The insert triggers the incremental materialized view `mv_inference_5m` (or `mv_tool_5m`). The Insights API reads from these MVs to serve the in-app `/insights` page at sub-10ms p95.

## SDK design

`InferenceLogger` (in `packages/chatbot-sdk/chatbot_sdk/client.py`) is the public entry point:

- `inference(...)` — async context manager that returns an `InferenceSpan`. Call `observe_chunk()` per streaming chunk (records ttft on first call, accumulates content + tokens), `set_response()` on non-streaming completion. On exit, the finalized log is handed to the transport.
- `tool_call(...)` — analogous context manager for tool dispatches. Spans accept a `parent_inference_request_id` so the consumer can stitch the timeline.
- `tool_traced(name=...)` — decorator that wraps an async function so each invocation emits a `tool_execution` log automatically.

The wire format lives in `chatbot_sdk.schema` (Pydantic). Each envelope carries `schema_version` so older clients are accepted via the ingestion-side migration map.

### Non-blocking transport

```
caller.submit(event)         # O(1) push to deque, returns immediately
   ↓
asyncio Event signals a background flusher task
   ↓
flusher batches up to batch_max (64) or flush_interval (1s) events
   ↓
HTTP POST with exponential-backoff retry (3 attempts × jittered delay)
   ↓
on permanent failure: increment failed_batches counter, drop, never raise
```

The deque is bounded (`maxlen=10_000`). On overflow, the **oldest** event is dropped (FIFO eviction) and a counter ticks. This is the right policy for high-volume telemetry: freshness wins over completeness, and a slow ingestor can't grow memory unbounded.

Retries use exponential backoff with jitter, three attempts total. After exhaustion the batch is dropped, a warning is emitted, and the flusher moves on. The chat path never sees logging failures.

`close()` drains the deque with a configurable timeout so process shutdown doesn't lose the tail of events.

### LangGraph integration

The chat service uses `langgraph.prebuilt.create_react_agent` for the model-plus-tools loop. We attach an `AsyncCallbackHandler` (in `chat-service/chat_service/llm/sdk_callback.py`) to each agent run. It produces one inference log per LLM call and one tool_execution per tool dispatch:

- `on_chat_model_start` → open an `InferenceSpan`, stash by `run_id`.
- `on_llm_new_token` → `span.observe_chunk(chunk)` to capture ttft + accumulate text + tokens.
- `on_chat_model_end` → finalize the span with the final `AIMessage` (extracts tokens, tool_calls, finish_reason).
- `on_tool_start` → open a `ToolSpan` parented to the last inference span's `request_id`.
- `on_tool_end` → finalize with the tool's return value.

Result: a single user turn that triggers `LLM → tool → LLM` produces **2 inference_logs + 1 tool_execution log**, all linked by `parent_inference_request_id`. The Insights API stitches these into a session timeline.

## Failure handling

What happens when each layer fails, in isolation:

- **Ingestion service is down.** The SDK queue fills, then drops the oldest events on overflow. The chat path keeps serving. When ingestion recovers, the queue drains. We lose the oldest dropped events. Acceptable for telemetry.
- **Valkey is down.** Ingestion returns 503 with `Retry-After: 2`. The SDK retries with exponential backoff. If the outage exceeds `queue_depth × event_rate`, oldest events drop. Consumers also pause until Valkey recovers; on recovery they resume from the last acked offset.
- **ClickHouse insert fails.** The consumer's bulk insert fails. The batch is published to the per-stream DLQ (`inference.v1.dlq`, `tool_executions.v1.dlq`, or `application.v1.dlq`) with the error attached, then ACKed on the original stream. Consumer continues processing live traffic. Operator drains DLQs after the root cause is fixed (typically schema drift or disk pressure).
- **Postgres is down.** The chat path 500s. This is the worst case here — chat state lives in Postgres. The production answer is a read replica + write retry shim. We don't ship that in the reference deployment; it belongs in the customer's HA story.
- **Insights API is down.** The in-app `/insights` page shows a graceful "no data" fallback. The ingestion side keeps writing — when the API comes back up, all the data is queryable again. ClickHouse remains a queryable surface for any operator who needs raw access.
- **Frontend is down.** The Compose health page shows the failure. Other vendor services running the SDK are unaffected.

The general principle: the chat path is the highest-priority code path. Every other layer can fail and recover without taking the chat path down with it.

## Idempotency

Each event has a `request_id` (UUIDv7, time-ordered). Three layers enforce exactly-once landing in ClickHouse:

1. **SDK transport** retries the **same envelope**, not new `request_id`s. Retries reuse the original UUID so the downstream dedup actually works.
2. **Ingestion** dedupes within a 10-minute window via `SET NX idem:<request_id>` in Valkey. Duplicates are accepted with `status='duplicate'` and never reach the broker.
3. **ClickHouse `ReplacingMergeTree`** collapses any duplicate that slips through, keyed by the table's `ORDER BY` (e.g. `(started_at, provider, model, request_id)` for `inference_logs`). The version with the largest `received_at` wins on background merge.

Net result: a `request_id` lands in ClickHouse exactly once even when the SDK retries a hung POST, the consumer dies between insert and ACK, or both happen on the same event.

## Scaling considerations

| Component         | Bottleneck                                                | Scale-up path |
|-------------------|-----------------------------------------------------------|---------------|
| chat-service      | Single-process Python, async. SDK queue is per-process.   | Horizontal — each replica has its own queue. Cancel registry would need Valkey pub/sub to fan out across replicas. |
| ingestion-service | Stateless. CPU-bound on PII (Presidio + spaCy).           | Horizontal. Add replicas + load balancer. PII can move to GPU-batched inference if throughput demands it. |
| Valkey Streams    | ~1M ops/sec on a single instance. Memory-bound.           | Partition streams by `hash(conversation_id) % N`. Or swap to Kafka — FastStream supports both with the same handler code. |
| inference-consumer | ClickHouse insert latency × batch size.                  | Already batches up to 200 events / 1s. Add replicas to the consumer group; ClickHouse handles the parallelism. |
| ClickHouse        | Single-node disk.                                         | Move to a 3-node sharded cluster (or ClickHouse Cloud). Monthly partitioning keeps a single shard fast even at billions of rows. |
| Postgres          | OLTP for chat state.                                      | Read replica for conversation list queries; write rate is bounded by user interactions. |
| insights-api      | ~10ms p95 reads from MV. CPU-bound on serialization.      | Stateless. Horizontal. Add a CDN cache for `/insights/summary`-style endpoints with a 30s TTL. |

## Security posture

**PII redaction at ingestion.** Presidio runs in the ingestion service before events hit the broker. Policy is centralized — we can upgrade recognizers without redeploying clients. Recognizers are env-configurable (`PII_RECOGNIZERS=EMAIL_ADDRESS,PHONE_NUMBER,...`). The **original** message body lives in Postgres `messages.content` (it's the user's own data; they own it). The redacted previews shipped to ClickHouse are what analysts and dashboards see.

**JWT in HttpOnly cookies.** `fastapi-users` issues a JWT, set as a `Secure; HttpOnly; SameSite=Lax` cookie. No tokens in localStorage means no XSS-based token theft. CSRF is mitigated by `SameSite=Lax` plus the JSON-only API surface (no form submits). Auth is verified on every request and the user is loaded into request state.

**No secrets in the repo.** `.env`, `.oracle/`, `kubeconfig*`, `*.pem`, `*.key`, and `secrets.env` are gitignored. `.env.example` ships placeholders; production values are injected per-environment (env file, vault, or k8s `Secret`).

**Why we explicitly avoided LiteLLM.** Two 2026 incidents:

1. **March 2026 PyPI supply-chain compromise.** `litellm==1.82.7` / `1.82.8` were live on PyPI for ~40 minutes serving malware that exfiltrated SSH keys, cloud credentials, and Kubernetes secrets from build hosts that pulled the affected versions. Mandiant was engaged for forensics.
2. **CVE-2026-42208 (April 2026).** SQL injection in LiteLLM Proxy's auth path, CVSS 9.3, exploited in the wild within 36 hours, added to CISA's KEV catalog. Fixed in v1.83.7+. Proxy-only — doesn't directly affect SDK usage, but is symptomatic of the project's exposure surface.

LangChain's per-provider chat-model packages each wrap the **official** provider SDK (`openai`, `anthropic`, `google-genai`). Smaller dependency tree, narrower attack surface, no comparable supply-chain history. We accept slightly more verbose model initialization for the security improvement.

**Caddy auto-HTTPS.** The production reverse proxy obtains certificates via ACME on first request and renews them automatically. No certbot cron jobs, no manual key rotation. HTTPS-only — Caddy serves a `308` redirect from `:80` to `:443`.

**Network isolation.** The dev Compose binds ports to `127.0.0.1` only. Production Compose exposes only `:80` and `:443` through Caddy; everything else stays on the Docker network.

## What's intentionally simple

- **No OpenTelemetry tracing.** Useful for cross-service correlation in larger deployments. We track `request_id` end-to-end, which buys most of the value at far less complexity.
- **No request rate limiting at ingestion.** Belongs in the ingestion service before broker publish. Straightforward to add (Valkey-backed token bucket) when traffic warrants it.
- **No multi-tenant compartments.** Users see only their own conversations; admins see all. There's no org/team layer yet.
- **No per-(provider, model) cost table.** We record cost only when the provider returns usage. A static price lookup would let us compute cost on every event.
