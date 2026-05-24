# Improvements after the initial take-home submission

Continuation of work after the first public commit. The original platform shipped
end-to-end (chat + ingestion + insights + admin) but the SDK was internal-only
and tightly coupled to chat-service. This phase made the SDK a standalone
PyPI-published artefact, added direct provider paths, isolated the chat vs
operator surfaces, and surfaced SDK overhead in the live dashboard.

**Live demo:**
- Chat: <https://chat.152.70.79.7.nip.io>
- Insights: <https://insights.152.70.79.7.nip.io>

**Published artefact:** <https://pypi.org/project/inferspect-sdk/0.2.2/>

**Source:** <https://github.com/itsdun1/inferspect>

---

## 1. SDK is now a real published package (`inferspect-sdk` on PyPI)

| Change | Notes |
|---|---|
| Renamed package from internal `chatbot-sdk` → public `inferspect-sdk` | Module name `chatbot_sdk` kept for backward compat |
| Pipecat-style optional extras | `[openai]`, `[anthropic]`, `[google]`, `[langchain]`, `[llamaindex]`, `[all]` |
| Three PyPI releases | `0.2.0` → `0.2.1` (stream token fix) → `0.2.2` (overhead metrics) |
| Real metadata on PyPI listing | MIT license, classifiers, project URLs, author, keywords, README |
| `py.typed` marker shipped | Customers get type hints from `inferspect-sdk` |
| Full README + CHANGELOG bundled | Listing page renders quickstart for each provider |

## 2. Auto-instrumentation for raw provider clients

| Integration | Pattern | Lines of customer code |
|---|---|---|
| OpenAI | `instrument(client, logger=)` — monkey-patches `chat.completions.create` | 3 |
| Anthropic | Same — patches `messages.create` | 3 |
| Google Gemini | Same — patches `aio.models.generate_content` | 3 |
| LangChain | `SDKCallback(sdk=logger)` (callback-driven, idiomatic to LangChain) | 4 |
| LlamaIndex | `LlamaIndexCallback(logger)` (callback) | 3 |
| Bug fix | Detection of async clients via `type(client).__name__.startswith("Async")` (since OpenAI's method isn't decorated `async def`, `inspect.iscoroutinefunction` was lying) |
| Idempotency | Sentinel attribute on wrapped methods — calling `instrument()` twice is a no-op |

## 3. Client-side PII redaction in the SDK

| Change | Notes |
|---|---|
| New `chatbot_sdk/pii.py` | Zero new deps — pure regex. Email, phone (E.164 + NA), CC (Luhn-validated), SSN, IBAN, IPv4, AWS access keys |
| Runs inside `_finalize()` | After span close but **before** transport submit — PII never leaves customer's process |
| Per-integration code | Zero — all integrations route through the same span finalize |
| Verified live | Phone numbers landed in ClickHouse as `<PHONE_NUMBER>` |

## 4. Multi-tenant tagging (server-side client resolution)

| Change | Notes |
|---|---|
| `SDK_API_KEYS_JSON` env-driven map | `{"osk_alice": "alice-corp", "osk_bob": "bob-inc"}` |
| New `ApiKeyResolver` in ingestion-service | Resolves `X-Sdk-Key` header → `client` name; 401 on unknown |
| ClickHouse `client LowCardinality(String)` column | On `inference_logs`, `tool_executions`, `application_logs` |
| Materialized views rebuilt | `mv_inference_5m` and `mv_tool_5m` now group by `(bucket, client, provider, model)` |
| `?client=` filter on every insights endpoint | `summary`, `latency`, `throughput`, `cost`, `errors`, `tools`, `top-conversations`, `anomalies`, `sessions` |
| SDK leaves `client` empty on the wire | Server-set only — SDK can't be trusted to self-identify |

## 5. AWS-style two-auth-surface split

| Change | Notes |
|---|---|
| Admin endpoints moved off chat-service | `/admin/users`, `/admin/conversations`, `/admin/synthetic` now live in insights-api |
| New operator auth in insights-api | fastapi-users with `console_session` cookie (separate from chat's `chat_session`) |
| Separate JWT secrets | `JWT_SECRET` for end-users, `CONSOLE_JWT_SECRET` for operators |
| No operator self-signup | Bootstrap via `CONSOLE_BOOTSTRAP_EMAIL` + `CONSOLE_BOOTSTRAP_PASSWORD` env |
| Postgres `operators` table | Separate from `users` table; SharedBase mapper for cross-tenant reads |

## 6. Frontend split — two Next.js apps

| Change | Notes |
|---|---|
| `apps/web-chat/` (port 3000) | End-user chat surface — auth via chat-service, `chat_session` cookie |
| `apps/web-insights/` (port 3002) | Operator console — auth via insights-api, `console_session` cookie |
| `apps/web-shared/` | TypeScript types only (~140 lines), npm workspace dep |
| `apps/web/` deleted | Old single app gone |
| Independent cookies | Logging out of one doesn't log out the other |
| Caddy subdomain routing | `chat.{$DOMAIN}` + `insights.{$DOMAIN}`, independent Let's Encrypt certs |
| 10-entry model dropdown | 6 LangChain paths + 4 direct-SDK paths |
| New "Client" filter dropdown | Slice metrics by tenant |
| New emerald "SDK Overhead" tile | p50/p95/p99/max + API p50 + overhead % |

## 7. Raw provider paths in chat-service (proves SDK works without LangChain)

| Change | Notes |
|---|---|
| `apps/chat-service/chat_service/llm/raw_providers.py` | NEW — direct OpenAI + Anthropic clients, instrumented via SDK |
| Model routing | `gpt-*` / `claude-*` / `gemini-*` → LangChain; `raw-openai/*`, `raw-anthropic/*` → direct |
| Streaming support | Both paths stream SSE to the chat UI |
| Conversation context | `logger.context(conversation_id=, user_id=)` ContextVar propagates per-request IDs |

## 8. SDK overhead self-measurement

| Change | Notes |
|---|---|
| `time.perf_counter()` boundaries | Pre-call, post-call, finalize-end captured in OpenAI + Anthropic wrappers + `_finalize` itself |
| New metadata fields | `sdk_pre_call_ms`, `sdk_finalize_ms`, `sdk_overhead_ms`, `api_call_ms` — stamped into `metadata` JSON |
| Zero schema change | Rides inside existing `metadata` column — customers upgrade with just `pip install -U` |
| New `/insights/sdk-overhead` endpoint | Returns p50/p95/p99/max via `JSONExtractFloat(metadata, 'sdk_overhead_ms')` |
| UI tile | Live shows `0.88 ms p99 on a 1609 ms API call (≈ 0.05 %)` |

## 9. Production Dockerfile that proves the PyPI install path

| Change | Notes |
|---|---|
| `apps/chat-service/Dockerfile` | NEW — standalone, doesn't touch workspace, `pip install .` resolves `inferspect-sdk[langchain]` from PyPI |
| Image tag `:pypi` | Visible signal that this build differs from workspace-based `:dev` images |
| Verified | `docker exec ollive-chat-service-1 pip show inferspect-sdk` shows `/usr/local/lib/python3.11/site-packages` |

## 10. Customer-facing examples (`packages/chatbot-sdk/examples/`)

| Example | What it shows |
|---|---|
| `openai_raw.py` | Auto-instrumented OpenAI |
| `anthropic_raw.py` | Auto-instrumented Anthropic |
| `gemini_raw.py` | Auto-instrumented Gemini |
| `langchain_agent.py` | LangChain callback |
| `llamaindex_query.py` | LlamaIndex callback |
| `sync_flask.py` | Synchronous Flask via `SyncInferenceLogger` |
| **`custom_llm.py`** (new) | Manual escape-hatch — `async with logger.inference(...)` per call |
| **`custom_llm_auto.py`** (new) | Tier 3 — customer writes their own `instrument()` using `wrap_method()` |

## 11. Operational fixes caught and shipped live

| Bug | Fix |
|---|---|
| OpenAI/Anthropic `instrument()` falsely detecting async clients as sync | Switched to `type(client).__name__.startswith("Async")` sniff |
| Pre-existing `admin@ollive.local` row breaks `/admin/users` due to `EmailStr` validator rejecting `.local` TLD | Switched `AdminUserDTO.email` and `OperatorRead.email` to plain `str` |
| Insights-api CORS blocked `:3002` (web-insights port) | Added `:3002` and `127.0.0.1:3002` to allow-list |
| Raw OpenAI streaming returning `tokens=0` | `_normalize_chunk` now reads the final chunk's `chunk.usage` (when `stream_options={"include_usage": True}`) |
| `NEXT_PUBLIC_INSIGHTS_API_URL` had trailing `/insights` — broke `/auth/login` routing | Changed to end at `/api`, rebuilt web-insights image |
| `ingestion-service` container not seeing `SDK_API_KEYS_JSON` → all events tagged `client=unknown` | Added env forwarding in compose; events now tagged correctly |
| Operator console redirected to wrong path | Caddy `handle /api/auth/* uri strip_prefix /api` routing fixed |

## 12. Deploy + repo hygiene

| Change | Notes |
|---|---|
| Oracle E5.Flex VM redeployed | All 11 containers rebuilt + restarted with new images |
| ClickHouse on Oracle | Volume nuked, fresh `init.sql` with `client` column applied |
| `.env` on Oracle | New keys appended (`SDK_API_KEYS_JSON`, `CONSOLE_*`, `NEXT_PUBLIC_*`) without touching existing secrets |
| GitHub `itsdun1/inferspect` | All commits pushed (one branch, public repo) |
| Tests | 138 workspace + 99 SDK = 237 passing, 1 skipped |

---

## Numbers

| Metric | Value |
|---|---|
| Pre-refactor tests | 65 |
| Post-refactor tests | **237** (138 workspace + 99 SDK) |
| Lines changed | ~5 200 insertions, ~770 deletions |
| New integration files in SDK | 6 (`openai.py`, `anthropic.py`, `google.py`, `langchain.py`, `llamaindex.py`, `_instrument.py`) |
| New SDK modules | 3 (`pii.py`, `sync.py`, `_require.py`) |
| New SDK examples | 8 total (2 brand-new for custom-LLM cases) |
| PyPI releases | 3 (`0.2.0`, `0.2.1`, `0.2.2`) |
| SDK overhead on a real call | **0.88 ms** total on a 1 609 ms API call (≈ 0.05 %) |

---

## Headline talking points

1. **"Real PyPI package, anyone can install it"** — <https://pypi.org/project/inferspect-sdk/0.2.2/>
2. **"3 lines to integrate"** — `examples/openai_raw.py` is 25 lines including imports
3. **"PII redaction never leaves your process"** — verified end-to-end with phone numbers
4. **"SDK adds 0.88 ms on a 1.6 s API call"** — emerald tile on the live insights dashboard
5. **"Multi-tenant by env config, no DB"** — one-line `SDK_API_KEYS_JSON` update onboards a new customer
6. **"chat-service is just an example customer"** — Docker container uses `pip install inferspect-sdk`, no workspace coupling, provable with `docker exec`
7. **"Two SDK patterns, one mental model"** — monkey-patch for raw providers, callbacks for frameworks, manual escape hatch for custom LLMs
8. **"Frontend split for separation of concerns"** — chat product + operator console live on different subdomains with independent cookies

---

## Files of interest

| Path | What's there |
|---|---|
| `packages/chatbot-sdk/` | The SDK source. Pure Python, no LangChain dep in core. |
| `packages/chatbot-sdk/chatbot_sdk/integrations/` | One file per supported provider |
| `packages/chatbot-sdk/examples/` | 8 customer-facing samples |
| `packages/chatbot-sdk/CHANGELOG.md` | Per-version diff |
| `apps/chat-service/Dockerfile` | Standalone build that pip-installs from PyPI |
| `apps/chat-service/chat_service/llm/sdk_integrations.py` | Single factory: instrumented OpenAI/Anthropic clients + LangChain callback builder |
| `apps/insights-api/insights_api/auth/` | Operator auth, separate cookie + JWT secret |
| `apps/web-chat/` and `apps/web-insights/` | The two Next.js apps |
| `infra/caddy/Caddyfile` | Subdomain routing + Let's Encrypt |
| `infra/docker-compose.prod.yml` | Production compose, used on Oracle VM |

---

## What the ingestion-service actually does

The ingestion-service is the **only** server the SDK talks to. It's the gate between customer processes and our ClickHouse store. Lives at `apps/ingestion-service/`. Every batch the SDK POSTs to `/v1/logs` goes through these stages, in order:

```
SDK POSTs batch  →  /v1/logs
        │
        ▼
  ╭─ AUTH (auth_service.ApiKeyResolver) ──────────────────╮
  │ Reads X-Sdk-Key header                                │
  │ Looks it up in SDK_API_KEYS_JSON env map              │
  │ Returns client_name (e.g. "chat-service") OR 401      │
  ╰───────────────────────────────────────────────────────╯
        │
        ▼
  ╭─ VALIDATE (validation_service) ───────────────────────╮
  │ Pydantic schema check per event                       │
  │ Rejects malformed events (bad provider, missing       │
  │ request_id, wrong log_type, etc.)                     │
  │ Per-event success/reject decision — bad ones drop,    │
  │ good ones continue                                    │
  ╰───────────────────────────────────────────────────────╯
        │
        ▼
  ╭─ IDEMPOTENCY (idempotency_repository) ────────────────╮
  │ For each event's request_id, mark_or_check() in       │
  │ Valkey with TTL. If already seen → drop (returns      │
  │ duplicates count in response). Protects against       │
  │ SDK retries that succeed on the second attempt        │
  │ (avoids double-counting).                             │
  ╰───────────────────────────────────────────────────────╯
        │
        ▼
  ╭─ TENANT STAMP (ingest_service) ───────────────────────╮
  │ Adds event["client"] = client_name resolved at auth   │
  │ stage. SDK left this empty; server is the source of   │
  │ truth (SDK can't be trusted to self-identify).        │
  ╰───────────────────────────────────────────────────────╯
        │
        ▼
  ╭─ PII REDACTION (pii_service, defense-in-depth) ───────╮
  │ Presidio-based regex pass on input_preview /          │
  │ output_preview / args_preview / result_preview /      │
  │ error_message. No-op if SDK already redacted (will    │
  │ just see <EMAIL_ADDRESS> tokens, leave them).         │
  │ Catches anything that slipped past the SDK.           │
  ╰───────────────────────────────────────────────────────╯
        │
        ▼
  ╭─ PUBLISH (valkey_publisher) ──────────────────────────╮
  │ XADD to Valkey stream:                                │
  │   - inference.v1       (for InferenceLog events)      │
  │   - tool_executions.v1 (for ToolExecutionLog)         │
  │   - application.v1     (for ApplicationLog)           │
  │ Stream entries trigger consumers downstream.          │
  ╰───────────────────────────────────────────────────────╯
        │
        ▼
  Return JSON to SDK:
    {"accepted": 4, "duplicates": 1, "rejected": 0,
     "events": [{ "request_id": ..., "status": "accepted" }, ...]}
```

After ingestion publishes to Valkey, the **consumers** (inference-consumer + app-log-consumer) pull from the streams and bulk-INSERT into ClickHouse. Ingestion never writes to ClickHouse directly — that's the consumer's job. This keeps ingestion's hot path tiny (every event hits Valkey in <2ms) and lets the slow ClickHouse writes happen in a separate process.

Stage-by-stage responsibilities:

| Stage | File | What it owns |
|---|---|---|
| Auth | `services/auth_service.py` (`ApiKeyResolver`) | Static env-driven `{api_key: client_name}` map. Single source of truth for which tenant each SDK key belongs to. |
| Validation | `services/validation_service.py` | Pydantic schema enforcement. Splits a mixed batch into accepted/rejected without rejecting the whole thing. |
| Idempotency | `repositories/idempotency_repository.py` | Valkey `SETNX request_id` with TTL. Per-event dedup. |
| Stamping | `services/ingest_service.py` (`ingest_batch`) | Orchestrates the pipeline; stamps `client`, `received_at`, returns the per-event status list. |
| PII | `services/pii_service.py` | Presidio (or regex fallback) — defense in depth behind SDK-side redaction. |
| Publish | `repositories/valkey_publisher.py` | Routes by `log_type` to one of three Valkey streams. |

The `/v1/logs` endpoint itself is just a FastAPI controller (`controllers/ingest_controller.py`) that wires Depends-injected auth + service + repository together and returns the response Pydantic model.

---

## Transport options — what we could ship next instead of HTTPS POST

Today the SDK ships logs over HTTPS POST to `/v1/logs`. That's the simplest universal transport but not the only choice. Realistic alternatives, ranked by relevance:

| Option | What it'd look like | Why it'd matter |
|---|---|---|
| **OTLP exporter** | SDK emits OpenTelemetry-protocol spans; customer routes them to us OR any OTLP collector (Honeycomb, Datadog, Jaeger, Grafana Tempo) | Removes vendor lock-in. Customer keeps the option to switch backends. Helicone pivoted to OTLP recently for exactly this. |
| **gRPC** | Same SDK→server flow, swap JSON over HTTPS for Protobuf over HTTP/2 | ~3× smaller payloads, multiplexed connections, native streaming. Matters at 10K+ events/sec. |
| **Local sidecar agent** | SDK writes JSON Lines to Unix socket or `localhost:8085`; an OpenTelemetry Collector / Vector / Fluent Bit sidecar handles the network | Customer process never makes outbound network calls. Common in K8s. Agent buffers to disk during ingestion outages. |
| **Message broker (Kafka / NATS / Redis Streams)** | SDK produces directly to broker; ingestion-service is just a consumer | Massive durability (broker retains for days). Ingestion can be down for hours, zero data loss. Adds broker creds to customer's env. |
| **Cloud-native pub/sub (Kinesis / Pub/Sub / Event Hubs)** | SDK uses cloud's managed queue; downstream consumer drains | Zero infrastructure for cloud-native customers, integrates with their IAM. Vendor-locked per cloud. |
| **WebSocket / SSE** | Persistent connection from SDK to ingestion, push events as they happen | Real-time dashboards (<1s end-to-end latency). Connection state to manage on both sides. |
| **UDP / StatsD-style** | Fire-and-forget UDP packets, ingestion reads from socket | Sub-millisecond send latency, no TCP handshake. **Not suitable** for inference logs (each event has billing value, can't tolerate silent packet loss). |
| **stdout + log shipper** | SDK `print()`s JSON Lines; container runtime captures stdout; existing log infra (Fluent Bit, Loki, Splunk) forwards | Zero new dependencies. Customer's existing log pipeline is reused. Higher latency, depends on their infra. |

The transport is abstracted behind `BatchedLogTransport` in the SDK (`packages/chatbot-sdk/chatbot_sdk/transport.py`), so adding a new one is a single class implementing the same `submit` / `start` / `close` interface. We could ship two transports side-by-side and let the customer pick via `ingestion_url` URI scheme (`https://...` vs `grpc://...` vs `otlp://...`).

**Most strategically valuable next step:** an OTLP exporter. It opens the SDK to the entire observability ecosystem and answers the "what if I want to use Honeycomb instead?" objection with "you can — same SDK, different exporter URL."
