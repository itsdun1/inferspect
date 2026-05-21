# Ollive — Design Document

This is the design document for the Ollive platform: an LLM observability product for AI vendors. It captures what we built and why — the architectural decisions, the data plane shape, the SDK contract, the storage split, and the deployment model.

## Context

Ollive is a working chatbot, a lightweight Python SDK that captures rich inference and tool-execution metadata, an ingestion pipeline, persistent storage in Postgres + ClickHouse, and dashboards. The full feature set ships in one repo: multi-provider LLM access, streaming, an event-driven backbone, PII redaction, a one-command Docker Compose setup, Kubernetes manifests, and a frontend with conversation cancel/list/resume.

Core decisions:

- **Focus.** The inference logging, ingestion, and analyzer subsystem is the centerpiece. The reference chatbot exists as a vehicle to generate realistic traffic; we keep it small.
- **Stack.** Python (FastAPI) for the backend and SDK; Next.js for the UI.
- **SDK.** Built from scratch. Multi-provider LLM calls go through **LangChain chat models** (`langchain-openai` / `-anthropic` / `-google-genai`). We explicitly avoid LiteLLM because of the March 2026 PyPI supply-chain compromise and the April 2026 CVE-2026-42208 SQL injection in LiteLLM Proxy (CVSS 9.3).
- **Primary provider.** Google Gemini. Multi-provider also covers OpenAI and Anthropic.
- **Licensing.** Strictly OSS for every dependency. The only non-OSS thing in the stack is the LLM API key.

Three log types share the same ingestion path:

1. **Inference logs** — rich, structured records of each LLM call (the headline feature).
2. **Tool execution logs** — every tool/function call dispatched in response to an LLM `tool_calls` finish — captures tool name, args, result, latency, status. Links back to the parent inference via `parent_inference_request_id`.
3. **Application logs** — standard structured logs from services (FastAPI, workers, UI server).

All three flow through the same ingestion endpoint, broker streams, and consumer pattern, but land in different ClickHouse tables with distinct schemas and dashboards.

---

## Architecture

```
┌──────────────────┐   SSE (stream)   ┌─────────────────────┐
│  Next.js UI      │ ───────────────► │  Chat Service       │
│  (Vercel AI SDK) │ ◄─────────────── │  FastAPI            │
│  cancel/list/    │   POST /chat     │  + inference SDK    │
│  resume          │                  │ + LangChain adapter  │
└──────────────────┘                  └─────────┬───────────┘
                                                │ async fire-and-forget
                                                │ (in-process queue → HTTP)
                                                ▼
                                      ┌─────────────────────┐
                                      │ Ingestion Service   │
                                      │ FastAPI             │
                                      │  - schema validate  │
                                      │  - PII redact       │
                                      │  - publish event    │
                                      └─────────┬───────────┘
                                                │
                                                ▼
                                      ┌─────────────────────┐
                                      │ Valkey Streams       │
                                      │ (event bus)         │
                                      │  inference.v1       │
                                      │  application.v1     │
                                      └────┬────────────┬───┘
                                           │            │
                              ┌────────────┘            └────────────┐
                              ▼                                       ▼
                  ┌────────────────────┐                    ┌────────────────────┐
                  │ Inference Consumer │                    │ App Log Consumer   │
                  │ (FastStream)       │                    │ (FastStream)       │
                  └──────────┬─────────┘                    └──────────┬─────────┘
                             │                                          │
                             ▼                                          ▼
                  ┌────────────────────┐                    ┌────────────────────┐
                  │ ClickHouse         │                    │ ClickHouse         │
                  │ inference_logs     │                    │ application_logs   │
                  └──────────┬─────────┘                    └────────────────────┘
                             │
                             ▼
                  ┌────────────────────┐                    ┌────────────────────┐
                  │ Insights API       │                    │ Grafana            │
                  │ FastAPI            │ ◄───── queries ──── │ dashboards         │
                  │ /insights/latency  │                    │ - latency p50/p95  │
                  │ /insights/errors   │                    │ - throughput       │
                  │ /insights/cost     │                    │ - error rate       │
                  └────────────────────┘                    │ - cost/model       │
                                                            └────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────┐
│ Postgres (OLTP): users, conversations, messages — durable chat state         │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Why two databases.** Postgres handles chat and conversation CRUD where we need transactions and joins. ClickHouse handles the high-volume, append-only analytics workload for inference logs and dashboards — Langfuse migrated off Postgres for the same reason, and ClickHouse is the proven choice here.

**Why Valkey Streams (not Kafka).** Lightweight, single container, fits a one-command Docker Compose setup. We use FastStream so the broker is swappable to Kafka or NATS later without code changes.

**SDK transport.** The SDK does *not* talk to the broker directly. It POSTs JSON to the ingestion service's HTTP endpoint with in-process batching, retry, and a bounded queue so it can't block or crash the chat path. Ingestion is the only thing that touches the broker. This keeps the SDK small and language-portable.

---

## Tech stack and OSS library choices

Every dependency below is OSS. The only proprietary thing in the system is the LLM API key.

| Layer | Choice | License | Why |
|---|---|---|---|
| LLM provider abstraction | **LangChain chat models** (`langchain-openai`, `langchain-anthropic`, `langchain-google-genai`) | MIT | Unified `BaseChatModel` interface, streaming + token counting + tool-call extraction. **We avoid LiteLLM** — see Security Note below. |
| Chat orchestration | **LangGraph** `create_react_agent` | MIT | Tool-calling ReAct loop, streaming via `astream_events("v2")`, no checkpointer (Postgres `messages` is the source of truth). |
| Conversation memory | Last-N messages loaded from Postgres + passed to the agent | — | The agent already takes a `messages` list directly, so we don't reach for `ConversationBufferWindowMemory`. |
| Web backend | **FastAPI** + **uvicorn** | MIT / BSD-3 | Native async, SSE-friendly, Pydantic validation. |
| Frontend | **Next.js 16** + **Vercel AI SDK** (`useChat`) | MIT / Apache-2.0 | Chat UI with streaming, `AbortController` cancel, message hydration. Used as a library, not a product. |
| Event broker | **Valkey** (Streams) via **FastStream** | BSD-3 / Apache-2.0 | Drop-in OSS replacement for Redis (Redis went SSPL in 2024). FastStream abstracts the broker so it's swappable to Kafka or NATS. |
| OLTP storage | **Postgres 16** + **SQLAlchemy 2** | PostgreSQL / MIT | Conversations and messages need transactions. |
| Analytics storage | **ClickHouse 24** + **clickhouse-connect** | Apache-2.0 | Inference logs at scale, columnar, materialized views. |
| PII redaction | **Microsoft Presidio** (analyzer + anonymizer) | MIT | Recognizers for SSN/CC/email/phone/name; pluggable. |
| Dashboards | **Grafana OSS** + ClickHouse datasource | AGPL-3.0 / Apache-2.0 | Standard, configurable, JSON-provisioned panels checked into the repo. |
| Structured logging | **structlog** + JSON output | Apache-2.0 / MIT | Application logs in the same shape we ingest. |
| Container orchestration | **Docker Compose** (dev + recommended single-VM prod) + **Kustomize** overlays for **k3s** | Apache-2.0 | One-command local; Kustomize keeps base manifests reusable across `local`/`staging`/`prod` overlays. |
| Testing | **pytest** + **pytest-asyncio** + **httpx** + **testcontainers** | MIT | Real Postgres / Valkey / ClickHouse in CI. |

We deliberately do **not** use Langfuse or Helicone even though both are OSS — the point of this product is to be the observability layer, not to vendor one. We borrow their proven storage choice (ClickHouse) but not their code.

### Security note — why no LiteLLM

LiteLLM is the most popular OSS multi-provider abstraction. We **deliberately excluded** it because of two incidents in 2026:

1. **PyPI supply-chain compromise (March 2026).** `litellm==1.82.7` and `1.82.8` were live on PyPI for ~40 minutes serving malware that exfiltrated SSH keys, cloud credentials, and Kubernetes secrets from build hosts that pulled the affected versions. Mandiant was engaged for forensics.
2. **CVE-2026-42208 (April 2026).** SQL injection in LiteLLM Proxy's auth path, CVSS 9.3, exploited in the wild within 36 hours, added to CISA's Known Exploited Vulnerabilities catalog. Fixed in v1.83.7+. Proxy-only — doesn't directly affect SDK usage, but is symptomatic of the project's exposure surface.

LangChain's per-provider chat-model packages each wrap the **official** provider SDK (`openai`, `anthropic`, `google-genai`) — smaller dependency tree, narrower attack surface, no comparable supply-chain history. We accept slightly more verbose model initialization for a meaningful security improvement.

---

## The inference SDK (`chatbot-sdk/`)

A thin Python package that wraps any LLM call and captures metadata.

**Public API:**

```python
from chatbot_sdk import InferenceLogger, traced
from langchain_google_genai import ChatGoogleGenerativeAI

logger = InferenceLogger(
    ingestion_url="http://ingestion:8001/v1/logs",
    service="chat-service",
    api_key=os.environ["SDK_KEY"],
)

@traced(logger, conversation_id=conv_id, session_id=sess_id)
async def call_llm(messages, model_name="gemini-2.5-pro"):
    llm = ChatGoogleGenerativeAI(model=model_name, streaming=True)
    async for chunk in llm.astream(messages):
        yield chunk

# Tool execution capture
@logger.tool_traced
async def search_web(query: str) -> str:
    ...
```

**What it captures (the JSON schema written to ingestion):**

```json
{
  "schema_version": "1.0",
  "log_type": "inference",
  "request_id": "uuid",
  "conversation_id": "uuid",
  "session_id": "uuid",
  "user_id": "uuid|null",
  "service": "chat-service",
  "provider": "google",
  "model": "gemini-2.5-pro",
  "started_at": "2026-05-21T10:00:00.000Z",
  "finished_at": "2026-05-21T10:00:02.341Z",
  "latency_ms": 2341,
  "ttft_ms": 412,
  "stream": true,
  "prompt_tokens": 142,
  "completion_tokens": 318,
  "total_tokens": 460,
  "cost_usd": 0.00234,
  "status": "ok | error | cancelled | timeout",
  "error_code": null,
  "error_message": null,
  "input_preview": "first 500 chars of last user message (redacted)",
  "output_preview": "first 500 chars of completion (redacted)",
  "finish_reason": "stop | tool_calls | length | content_filter | error",
  "tool_calls_count": 0,
  "tool_calls_summary": [{"name": "search_web", "args_preview": "..."}],
  "metadata": {
    "temperature": 0.7,
    "max_tokens": 1024,
    "client_ip_hash": "...",
    "user_agent": "..."
  }
}
```

**Tool execution log schema:**

```json
{
  "schema_version": "1.0",
  "log_type": "tool_execution",
  "request_id": "uuid",
  "tool_call_id": "uuid (from model response)",
  "parent_inference_request_id": "uuid",
  "conversation_id": "uuid",
  "session_id": "uuid",
  "user_id": "uuid|null",
  "service": "chat-service",
  "tool_name": "search_web",
  "started_at": "...",
  "finished_at": "...",
  "latency_ms": 234,
  "status": "ok | error | timeout",
  "error_code": null,
  "error_message": null,
  "args_preview": "first 500 chars of JSON args (redacted)",
  "result_preview": "first 500 chars of result (redacted)",
  "result_size_bytes": 1234,
  "metadata": {}
}
```

**Design choices for the SDK:**

- **Non-blocking.** An in-process `asyncio.Queue` + background flusher batches up to N events or T ms, whichever comes first. Chat latency never pays for logging.
- **Bounded queue + drop policy.** If ingestion is down, the queue fills, we drop oldest, and emit a warning counter — never block the chat path.
- **Retries.** Exponential backoff with jitter, three attempts, then drop and log.
- **Streaming hooks.** SDK records `ttft_ms` on the first chunk, `latency_ms` on stream completion, and handles `cancelled` status if the caller aborts.
- **Provider-agnostic.** Works whether you call OpenAI / Anthropic / Gemini directly or via any abstraction. Provider / model strings pass through.
- **Application logs.** The same package exposes a `structlog` processor that ships application logs to the same ingestion endpoint with `log_type: "application"`.

---

## Data model

### Postgres — chat and conversation state (OLTP)

```sql
users (
  id UUID PK, email TEXT UNIQUE, created_at TIMESTAMPTZ
)

conversations (
  id UUID PK,
  user_id UUID FK,
  title TEXT,
  status TEXT CHECK (status IN ('active','cancelled','completed')),
  model TEXT,           -- last model used (denormalized for list view)
  message_count INT DEFAULT 0,
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ
)
CREATE INDEX ON conversations (user_id, updated_at DESC);

messages (
  id UUID PK,
  conversation_id UUID FK,
  role TEXT CHECK (role IN ('user','assistant','system','tool')),
  content TEXT,                    -- original
  content_redacted TEXT,           -- post-Presidio (what's safe to show in logs)
  inference_request_id UUID,       -- links to ClickHouse row
  status TEXT,                     -- 'complete' | 'streaming' | 'cancelled' | 'error'
  created_at TIMESTAMPTZ
)
CREATE INDEX ON messages (conversation_id, created_at);
```

### ClickHouse — analytics (OLAP)

```sql
CREATE TABLE inference_logs (
  request_id UUID,
  conversation_id UUID,
  session_id UUID,
  user_id Nullable(UUID),
  service LowCardinality(String),
  provider LowCardinality(String),
  model LowCardinality(String),
  started_at DateTime64(3),
  finished_at DateTime64(3),
  latency_ms UInt32,
  ttft_ms Nullable(UInt32),
  stream UInt8,
  prompt_tokens UInt32,
  completion_tokens UInt32,
  total_tokens UInt32,
  cost_usd Float64,
  status LowCardinality(String),
  error_code Nullable(String),
  error_message Nullable(String),
  input_preview String,
  output_preview String,
  metadata JSON
) ENGINE = MergeTree
PARTITION BY toYYYYMM(started_at)
ORDER BY (started_at, provider, model, conversation_id)
TTL toDateTime(started_at) + INTERVAL 90 DAY;

CREATE TABLE tool_executions (
  request_id UUID,
  tool_call_id UUID,
  parent_inference_request_id UUID,
  conversation_id UUID,
  session_id UUID,
  user_id Nullable(UUID),
  service LowCardinality(String),
  tool_name LowCardinality(String),
  started_at DateTime64(3),
  finished_at DateTime64(3),
  latency_ms UInt32,
  status LowCardinality(String),
  error_code Nullable(String),
  error_message Nullable(String),
  args_preview String,
  result_preview String,
  result_size_bytes UInt32,
  metadata JSON
) ENGINE = MergeTree
PARTITION BY toYYYYMM(started_at)
ORDER BY (started_at, tool_name, conversation_id)
TTL toDateTime(started_at) + INTERVAL 90 DAY;

CREATE TABLE application_logs (
  ts DateTime64(3),
  level LowCardinality(String),
  service LowCardinality(String),
  trace_id Nullable(UUID),
  span_id Nullable(String),
  message String,
  attributes JSON
) ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (ts, service, level)
TTL toDateTime(ts) + INTERVAL 30 DAY;
```

Materialized views for dashboards (pre-aggregated, cheap reads):

```sql
CREATE MATERIALIZED VIEW mv_latency_5m ENGINE = AggregatingMergeTree
ORDER BY (bucket, provider, model) AS
SELECT
  toStartOfFiveMinute(started_at) AS bucket,
  provider, model,
  quantileState(0.5)(latency_ms)  AS p50_state,
  quantileState(0.95)(latency_ms) AS p95_state,
  quantileState(0.99)(latency_ms) AS p99_state,
  countState() AS req_count,
  sumState(total_tokens) AS tokens_state,
  sumState(cost_usd) AS cost_state,
  countStateIf(status = 'error') AS error_count
FROM inference_logs
GROUP BY bucket, provider, model;
```

---

## Service architecture pattern — Controller / Service / Repository

Every Python service follows the same three-layer pattern with **explicit transaction passing**:

```
HTTP / Broker
     │
     ▼
┌──────────────┐   request validation, auth, dependency injection;
│ Controller   │   thin — translates between HTTP/broker and service calls;
│ (router)     │   never touches the DB directly
└──────┬───────┘
       │ (session: AsyncSession, current_user, dto)
       ▼
┌──────────────┐   business logic, orchestration across repositories,
│ Service      │   policy enforcement, never knows about FastAPI / SSE / Valkey;
│              │   accepts a session argument, never opens its own
└──────┬───────┘
       │ (session, query/command args)
       ▼
┌──────────────┐   the only layer that talks to Postgres / Valkey / ClickHouse;
│ Repository   │   accepts a session, performs query/insert/update;
│              │   returns ORM models or domain objects
└──────────────┘
```

**Transaction boundary = request.** The session is created by a FastAPI dependency (`get_session`), committed on success and rolled back on exception, and closed in a `finally`. Repositories never call `await session.commit()` — that's the dependency's job. This keeps transactions composable: a service can call multiple repositories within the same unit of work.

```python
# db/session.py
async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

# controllers/chat_controller.py
@router.post("/chat")
async def send_message(
    body: SendMessageRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    return await chat_service.send_message(session=session, user=user, body=body)

# services/chat_service.py
async def send_message(*, session, user, body):
    convo = await conversation_repo.get_for_user(session, body.conversation_id, user.id)
    await message_repo.create(session, conversation_id=convo.id, role="user", content=body.content)
    ...

# repositories/message_repository.py
async def create(session, *, conversation_id, role, content) -> Message:
    msg = Message(conversation_id=conversation_id, role=role, content=content)
    session.add(msg)
    await session.flush()
    return msg
```

Applied across all services:

- **chat-service** — controllers, services, repositories (Postgres via SQLAlchemy AsyncSession).
- **ingestion-service** — controllers, services (validate + PII), publishers (Valkey Streams) playing the repository role.
- **consumers** — broker subscriptions are the "controllers", services do batching, writers are the repository layer (ClickHouse).
- **insights-api** — controllers, services (aggregation + anomaly detection), repositories (ClickHouse queries).

---

## Service layout (monorepo)

```
ovel_assi/
├── apps/
│   ├── web/                    # Next.js 16 + Vercel AI SDK
│   │   ├── app/
│   │   │   ├── page.tsx                  # conversation list
│   │   │   └── chat/[id]/page.tsx        # chat view, cancel/resume
│   │   └── lib/api.ts                    # backend client
│   ├── chat-service/                   # FastAPI: /chat (SSE), conversations CRUD, auth
│   │   ├── chat_service/
│   │   │   ├── main.py                       # app factory, lifespan
│   │   │   ├── config.py                     # pydantic-settings
│   │   │   ├── deps.py                       # get_session, current_user, require_admin
│   │   │   ├── schemas.py                    # API DTOs (Pydantic)
│   │   │   ├── cancellation.py               # active-stream registry
│   │   │   ├── db/{session,models}.py        # AsyncSession factory + ORM
│   │   │   ├── controllers/                  # routers (HTTP only)
│   │   │   ├── services/                     # business logic; take session as arg
│   │   │   ├── repositories/                 # only layer that touches Postgres
│   │   │   └── llm/
│   │   │       ├── langchain_adapter.py      # multi-provider wrapper
│   │   │       └── memory.py                 # ConversationBufferWindowMemory glue
│   ├── ingestion-service/              # FastAPI: /v1/logs ingest
│   │   ├── ingestion_service/
│   │   │   ├── main.py
│   │   │   ├── config.py
│   │   │   ├── deps.py
│   │   │   ├── schemas.py                    # log envelope schemas (per log_type)
│   │   │   ├── controllers/ingest_controller.py
│   │   │   ├── services/
│   │   │   │   ├── validation_service.py     # schema_version migration map
│   │   │   │   ├── pii_service.py            # Presidio analyzer + anonymizer
│   │   │   │   └── ingest_service.py         # orchestration
│   │   │   └── repositories/
│   │   │       ├── valkey_publisher.py       # publishes to streams
│   │   │       └── idempotency_repository.py # SET NX dedup
│   ├── inference-consumer/             # FastStream worker → ClickHouse
│   ├── app-log-consumer/               # FastStream worker → ClickHouse
│   └── insights-api/                   # FastAPI: /insights/{latency,errors,cost,...}
├── packages/
│   └── chatbot-sdk/            # the Python SDK
│       ├── chatbot_sdk/
│       │   ├── client.py                 # InferenceLogger
│       │   ├── decorators.py             # @traced
│       │   ├── streaming.py              # ttft + cancel hooks
│       │   ├── transport.py              # batched HTTP w/ retries
│       │   └── structlog_processor.py    # app log shipping
│       └── tests/
├── infra/
│   ├── docker-compose.yml      # dev infra (4 containers)
│   ├── docker-compose.prod.yml # full stack + Caddy
│   ├── caddy/Caddyfile
│   ├── grafana/
│   ├── clickhouse/init.sql
│   └── k8s/                    # Kustomize: base + overlays (local k3d, prod k3s)
└── docs/
```

---

## Chatbot — kept intentionally simple

The chatbot exists to generate realistic inference traffic; it is **not** where we spend our engineering budget. Scope:

- **Memory.** Last-N messages loaded from Postgres on each request and passed to the LangGraph agent directly.
- **UI.** A single Next.js page with a model dropdown, message list, input box, "new chat" button, and a sidebar listing past conversations. Tailwind for styling. Vercel AI SDK `useChat` handles streaming + abort.
- **Cancel / list / resume.**
  - *Active-stream registry.* chat-service keeps `dict[conversation_id, asyncio.Task]`. `POST /conversations/{id}/cancel` calls `task.cancel()`. The SSE generator catches `CancelledError`, persists the partial assistant message with `status='cancelled'`, and the SDK flushes an inference log with `status='cancelled'` and whatever tokens it had so far.
  - *List.* `GET /conversations?user_id=X&limit=20&cursor=...` — id, title, status, model, last_updated, message_count. Postgres index on `(user_id, updated_at DESC)`.
  - *Resume.* `GET /conversations/{id}/messages` returns the transcript; UI hydrates `useChat`'s `initialMessages`. Cancelled messages render with a `(cancelled)` badge.

## Authentication and roles

Two roles, enforced by **`fastapi-users`** (MIT, OSS):

| Role | Capabilities |
|---|---|
| `user` | Register, login, create / list / resume / cancel own conversations, chat. Cannot see other users' data. |
| `admin` | Everything in `user`, plus: list all conversations, view insights dashboards (`/admin/insights`), view application + inference logs, generate synthetic inference logs (`POST /admin/synthetic`). |

Implementation:

- `users` table adds `role TEXT NOT NULL DEFAULT 'user'` (CHECK in {`user`, `admin`}) and `hashed_password TEXT NOT NULL`.
- `fastapi-users` provides `/auth/register`, `/auth/login`, `/auth/logout`, password reset. JWT in HttpOnly cookies (no localStorage — avoids XSS token theft).
- FastAPI dependency `require_admin = Depends(current_active_user_with_role("admin"))` gates admin endpoints.
- Bootstrap admin from env vars at startup: `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD`. Idempotent — only created if missing.
- Frontend: Next.js `middleware.ts` checks the cookie and redirects unauthenticated routes to `/login`. Admin routes (`/admin/*`) additionally check `role === 'admin'`.
- Server-side: every API call carries the auth cookie; the backend resolves the user and enforces row-level access (`conversation.user_id == current_user.id` OR `current_user.role == 'admin'`).

Admin UI routes:

- `/admin` — overview with throughput / latency / error / cost tiles
- `/admin/conversations` — all conversations across users (PII-redacted previews)
- `/admin/users` — list users, change roles
- `/admin/insights` — full charts from the Insights API
- `/admin/synthetic` — form to generate batches of synthetic inference logs

User-facing routes:

- `/login`, `/register`
- `/chat` — conversation list + active chat view (the main app)

---

## PII redaction

- Runs inside the **ingestion service**, not the SDK. Centralized policy is easier to upgrade without redeploying clients.
- Presidio analyzer runs over `input_preview`, `output_preview`, and any string field in `metadata`. The anonymizer replaces detected spans with `<EMAIL>`, `<PHONE>`, etc.
- The **original** message body is stored in Postgres `messages.content` (the user's own data, they own it). The **redacted** version (`content_redacted`) and the previews in ClickHouse are what dashboards and analysts see.
- Configurable allow-list of recognizers via env (`PII_RECOGNIZERS=…`) so policy is tunable without code changes.

---

## Log ingestion pipeline (the centerpiece)

The headline part of the system. Three independent services, each with a clear contract.

### 1. Ingestion service (the receiver)

`POST /v1/logs` — accepts a batch of log envelopes from any SDK client.

```
Request envelope:
{
  "service": "chat-service",
  "sdk_version": "0.1.0",
  "events": [ { log_type: "inference" | "tool_execution" | "application", ...payload }, ... ]
}
```

Per-event pipeline:

1. **Schema validation.** Pydantic models per `log_type`. Reject unknown `log_type` with 422. Schema is versioned (`schema_version` field); old versions accepted via a migration map.
2. **Enrichment.** Server-side `received_at` timestamp, ingestion node id, normalized `provider` casing, derived fields (`is_error`, `cost_usd_estimated` when the SDK didn't compute one).
3. **PII redaction.** Presidio runs on string fields per `log_type` policy.
4. **Idempotency.** Events carry `request_id` (UUIDv7). Ingestion dedupes within a 10-minute window via Valkey SET NX before publishing.
5. **Publish.** Event published to `inference.v1`, `tool_executions.v1`, or `application.v1` Valkey Stream with maxlen capping. The HTTP response returns 202 with per-event accept/reject status.

The ingestion service is **stateless** and horizontally scalable. Failure mode: if Valkey is down, returns 503 with `Retry-After`; the SDK queues and retries.

### 2. Consumers (the writers)

Two FastStream workers, one per stream family, scaled independently:

- `inference-consumer` — reads `inference.v1` + `tool_executions.v1`, batches ~200 events or 1s, bulk-inserts into the matching ClickHouse table. On insert failure: re-publish to the DLQ stream (`*.v1.dlq`) with the error attached; alert via app log.
- `app-log-consumer` — symmetric, writes to `application_logs`.

Consumer groups give us at-least-once delivery; idempotency is enforced at write time by ClickHouse's `ReplacingMergeTree` keyed on `request_id`.

### 3. Insights API + log analyzer

A small FastAPI service that exposes opinionated queries backed by ClickHouse (mostly `SELECT` from materialized views — millisecond responses):

- `GET /insights/latency?window=1h&group=model` → p50 / p95 / p99 per model per 5-min bucket
- `GET /insights/throughput?window=1h` → requests/min, tokens/min, per provider
- `GET /insights/errors?window=24h` → counts + sample messages grouped by `error_code` and `provider`
- `GET /insights/cost?window=7d&group=model` → spend per model, top-spend conversations
- `GET /insights/top-conversations?metric=latency&limit=20` → outlier conversations
- `GET /insights/sessions/{session_id}` → full timeline for one session (all inference + app logs interleaved)
- `GET /insights/summary?window=1h` → single rollup JSON used by the UI's "health" badge
- `GET /insights/anomalies?window=1h` → z-score anomaly detection on latency and error-rate per (provider, model) bucket

Grafana panels hit the same MVs directly. The insights API is for the UI's "Inference" tab and any programmatic consumer.

### Insights mini-UI

A second route in the Next.js app (`/insights`) renders charts from the Insights API: a latency chart, an error-rate chart, a cost-per-model chart, and a recent-sessions table that drills into the session timeline. Recharts (MIT) for charts.

---

## Deployment

**Docker Compose (`make up`)** — single command brings up: postgres, clickhouse, valkey, grafana, chat-service, ingestion-service, inference-consumer, app-log-consumer, insights-api, web. Grafana is provisioned with the dashboards and datasource on first start.

**Self-hosted k8s with Kustomize** — structure:

```
infra/k8s/
├── base/                       # raw, environment-agnostic manifests
│   ├── namespace.yaml
│   ├── chat-service/           # Deployment + Service + ConfigMap
│   ├── ingestion-service/
│   ├── inference-consumer/
│   ├── app-log-consumer/
│   ├── insights-api/
│   ├── web/
│   ├── postgres/               # StatefulSet + Service + PVC
│   ├── clickhouse/             # StatefulSet + Service + PVC
│   ├── valkey/                 # StatefulSet + Service + PVC
│   ├── grafana/                # Deployment + Service + ConfigMap (dashboards)
│   ├── ingress.yaml            # Traefik (bundled with k3s)
│   └── kustomization.yaml
└── overlays/
    ├── local/                  # k3d on dev laptop — NodePort, no TLS, ephemeral volumes
    │   └── kustomization.yaml
    └── prod/                   # VM running k3s — Let's Encrypt TLS, hostPath PVs
        ├── kustomization.yaml
        ├── secrets.env         # gitignored, only references
        └── ingress-patch.yaml  # adds real domain + cert-manager annotations
```

Why Kustomize over Helm: pure-YAML, no templating engine, no values.yaml indirection. Overlays patch the base directly.

**Single-VM production — Docker Compose + Caddy**

The recommended production path runs **Docker Compose on a single VM, fronted by Caddy with automatic Let's Encrypt**. Kustomize manifests stay in the repo for users who already run k3s/k8s, but Compose on a VM is the simpler operational path.

Deploy runbook:

1. Provision a small ARM or x86 VM.
2. Install Docker + Compose plugin.
3. `git clone` + `cp .env.example .env`. Fill `GEMINI_API_KEY`, `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD`, `JWT_SECRET`, `DOMAIN=your.host`.
4. `docker compose -f infra/docker-compose.prod.yml up -d`.
5. Caddy reverse-proxies the Next.js frontend on `:443` with auto Let's Encrypt — no separate cert-manager step.
6. Open `https://${DOMAIN}` → login screen.

The `infra/docker-compose.prod.yml` is a thin overlay on the dev compose: adds Caddy, removes port exposures except `:80` and `:443`, uses named volumes for persistence, pins image tags.

---

## Implementation phases

Ordered to land the *focus* (logging + ingestion + analyzer) early, with the chatbot as the traffic source.

1. **Foundation.** Repo scaffold, docker-compose with postgres/valkey/clickhouse/grafana, healthchecks, structlog config, k3d-ready images.
2. **SDK v1.** `InferenceLogger`, `@traced` decorator, batched non-blocking HTTP transport with retry/drop, schema definition, unit tests with mocked ingestion.
3. **Ingestion service.** `POST /v1/logs`, Pydantic per-`log_type` validation, Presidio PII pipeline, idempotency, publish to Valkey Streams with DLQ on overflow.
4. **Consumers.** Inference + app-log consumers via FastStream, ClickHouse bulk inserts, `ReplacingMergeTree` for idempotency, DLQ streams.
5. **Minimal chat service.** FastAPI `/chat` non-streaming, Postgres conversations/messages, LangChain with Gemini, last-N memory. SDK wired in.
6. **Insights API + ClickHouse MVs.** The analyzer service. All `/insights/*` endpoints.
7. **Grafana dashboards.** Four JSON-provisioned dashboards (latency, throughput, errors, cost) checked into the repo, plus an app-logs dashboard.
8. **Streaming + cancel.** Switch chat to SSE; active-stream registry; cancel endpoint; SDK records `ttft_ms` + emits `cancelled` status.
9. **Multi-provider.** Add OpenAI and Anthropic; model selector in UI.
10. **Frontend.** Next.js chat page (list/resume/cancel) + `/insights` route with Recharts.
11. **Auth + roles.** `fastapi-users` integration, `users.role`, bootstrap admin, admin endpoints + `/admin/*` UI routes, route gating in Next.js middleware.
12. **Kustomize manifests.** Base + overlays/{local, prod} committed.
13. **Single-VM production deploy.** Docker Compose + Caddy on a small VM with the production overlay.
14. **README + architecture notes.**

---

## Verification

End-to-end smoke test (manual + a pytest version checked into the repo):

1. `docker compose up -d`; wait for healthchecks.
2. `curl POST /conversations` → conversation id.
3. Open `http://localhost:3000/chat/<id>`, send a message → see streaming Gemini response.
4. Send another message mid-stream and hit cancel → UI shows cancelled state.
5. Refresh page → conversation list shows it; click → transcript reloads.
6. Switch model in UI to OpenAI or Anthropic → same behavior.
7. Inject test PII (`my email is alice@example.com, phone 555-123-4567`) → confirm Postgres `content` has it but `content_redacted` and ClickHouse `input_preview` have `<EMAIL>` / `<PHONE_NUMBER>`.
8. `curl http://localhost:8003/insights/latency?window=1h` → returns p50/p95/p99.
9. Open Grafana at `:3001`, dashboards show traffic from steps 3–6.
10. Kill ingestion-service for 30s mid-conversation → SDK keeps queueing; restart; queue drains; no chat-side errors.
11. `kubectl apply -k infra/k8s/overlays/local` against a k3d cluster; repeat step 3 against the cluster ingress.

The automated test suite covers SDK transport (backoff, drop, batch), ingestion validation + PII, consumer idempotency, conversation cancel state transitions, and insights query correctness.

---

## Critical files

- `packages/chatbot-sdk/chatbot_sdk/client.py` — SDK entry point
- `packages/chatbot-sdk/chatbot_sdk/transport.py` — batched HTTP with retry and drop
- `apps/chat-service/main.py` + `routes/chat.py` — SSE streaming + cancellation registry
- `apps/chat-service/llm/langchain_adapter.py` — multi-provider streaming wrapper
- `apps/ingestion-service/main.py` + `pii.py` — validation + Presidio
- `apps/inference-consumer/main.py` — FastStream → ClickHouse
- `apps/insights-api/main.py` — analytics endpoints
- `apps/web/app/chat/[id]/page.tsx` — `useChat` + cancel/resume UX
- `infra/docker-compose.yml` + `infra/docker-compose.prod.yml` (Caddy overlay)
- `apps/chat-service/auth.py` — `fastapi-users` wiring, role dependencies, bootstrap admin
- `apps/web/middleware.ts` — auth + admin route gating
- `infra/grafana/dashboards/*.json`
- `infra/clickhouse/init.sql`
- `infra/k8s/base/` + `infra/k8s/overlays/{local,prod}/`
- `README.md` + `ARCHITECTURE.md`
