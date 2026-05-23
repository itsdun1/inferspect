# Changelog

## 0.2.2 — 2026-05-23

### Added
- Self-measured SDK overhead metrics. Every inference event now ships with
  `metadata.sdk_overhead_ms` (pre-call setup + finalize work) and
  `metadata.api_call_ms` (the actual provider HTTP call). Lets customers
  prove the SDK adds ~0 latency vs the raw provider call.
- Measured via `time.perf_counter()` boundaries in OpenAI and Anthropic
  integration wrappers, plus a perf boundary inside `InferenceSpan._finalize`
  that covers PII redaction + Pydantic model construction.

### Behaviour
- No wire-format change. The new fields live inside the existing
  `metadata` JSON column, so customers upgrading to 0.2.2 see the metrics
  in their backend immediately with no server-side coordination.

## 0.2.1 — 2026-05-23

### Fixed
- `chatbot_sdk.integrations.openai._normalize_chunk` now reads `chunk.usage`
  when present. With `stream_options={"include_usage": True}` set on the
  patched `chat.completions.create`, the final streaming chunk carries the
  token totals — previously they were dropped, leading to `prompt_tokens=0`
  and `completion_tokens=0` for auto-instrumented OpenAI streams.

## 0.2.0 — 2026-05-23

First public release.

### Added
- Optional extras for vendor integrations: `[openai]`, `[anthropic]`, `[google]`, `[langchain]`, `[llamaindex]`, `[all]`. Core install has zero LLM-library dependencies.
- `chatbot_sdk.integrations.openai.instrument(client, logger=)` — monkey-patches `AsyncOpenAI.chat.completions.create` for auto-tracing. Idempotent.
- `chatbot_sdk.integrations.anthropic.instrument(client, logger=)` — same for `AsyncAnthropic.messages.create`.
- `chatbot_sdk.integrations.google.instrument(client, logger=)` — same for `google-genai`'s `aio.models.generate_content` and `generate_content_stream`.
- `chatbot_sdk.integrations.langchain.SDKCallback` — `AsyncCallbackHandler` for LangChain pipelines. Reads per-request IDs from `logger.context(...)` ContextVar.
- `chatbot_sdk.integrations.llamaindex.LlamaIndexCallback` — `BaseCallbackHandler` for LlamaIndex.
- `chatbot_sdk.SyncInferenceLogger` — synchronous facade for Flask, Django sync views, scripts, notebooks. Runs an asyncio loop on a daemon thread internally.
- Client-side PII redaction in `chatbot_sdk.pii` — pure regex recognizers for email, phone (E.164 + NA), credit card (Luhn), US SSN, IBAN, IPv4, IPv6, AWS access keys. Enabled by default via `InferenceLogger(pii_redact=True)`; configurable via `pii_recognizers=[...]`.
- `InferenceLogger.from_env()` — reads `CHATBOT_SDK_URL`, `CHATBOT_SDK_KEY`, `CHATBOT_SDK_SERVICE`.
- `async with logger:` lifecycle replaces explicit `start()` / `close()`.
- `logger.context(*, conversation_id=, session_id=, user_id=)` async context manager propagates per-request IDs via `contextvars.ContextVar`. Integrations read it via `chatbot_sdk.client.current_context()`.
- `chatbot_sdk._require._require_extra(extra, *modules)` helper for integration modules to gate vendor imports with a `pip install inferspect-sdk[...]` hint.
- `examples/` — runnable customer-facing examples for each integration.

### Changed
- Renamed package from `chatbot-sdk` (internal) to `inferspect-sdk` (public).
- `client: str` field added to `InferenceLog`, `ToolExecutionLog`, `ApplicationLog` — server-set tenant tag (SDK leaves it empty).

### Removed
- Hard dependency on `langchain-core`. The LangChain bridge is now an opt-in extra.
