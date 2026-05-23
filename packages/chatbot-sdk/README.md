# inferspect-sdk

Lightweight Python SDK for capturing LLM inference logs, tool executions, and application logs — with **client-side PII redaction** so sensitive data never leaves your process unredacted.

Auto-instruments raw OpenAI / Anthropic / Google Gemini clients, plus framework integrations for LangChain and LlamaIndex. Sync and async APIs. No telemetry vendor lock-in — point it at any HTTPS ingestion endpoint that accepts the wire format.

## Install

```bash
# Core only (no framework deps pulled in)
pip install inferspect-sdk

# With your stack:
pip install "inferspect-sdk[openai]"
pip install "inferspect-sdk[anthropic]"
pip install "inferspect-sdk[google]"
pip install "inferspect-sdk[langchain]"
pip install "inferspect-sdk[llamaindex]"

# Everything
pip install "inferspect-sdk[all]"
```

## Quickstart — raw OpenAI

```python
import asyncio
from openai import AsyncOpenAI
from chatbot_sdk import InferenceLogger
from chatbot_sdk.integrations.openai import instrument

async def main():
    client = AsyncOpenAI()
    logger = InferenceLogger.from_env()   # reads CHATBOT_SDK_URL + CHATBOT_SDK_KEY
    instrument(client, logger=logger)     # ← one-line monkey-patch; idempotent

    async with logger:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Say hello"}],
        )
        print(resp.choices[0].message.content)
        # Every call is auto-traced — no per-call wrapping.

asyncio.run(main())
```

Set two env vars before running:

```bash
export CHATBOT_SDK_URL=https://your-ingestion-host/v1/logs
export CHATBOT_SDK_KEY=your-sdk-api-key
```

## Quickstart — LangChain

```python
from chatbot_sdk import InferenceLogger
from chatbot_sdk.integrations.langchain import SDKCallback

logger = InferenceLogger.from_env()
await logger.start()

callback = SDKCallback(sdk=logger)
result = await agent.ainvoke(
    {"messages": msgs},
    config={"callbacks": [callback]},   # every LLM + tool call inside is auto-traced
)
```

## Quickstart — sync (Flask / Django / notebooks)

```python
from chatbot_sdk import SyncInferenceLogger

logger = SyncInferenceLogger.from_env()
with logger:
    with logger.inference(provider="openai", model="gpt-4o-mini") as span:
        resp = client.chat.completions.create(...)
        span.set_response(resp)
```

## What gets logged

Per inference: `provider`, `model`, `prompt_tokens`, `completion_tokens`, `latency_ms`, `ttft_ms` (streaming), `status` (`ok`/`error`/`cancelled`/`timeout`), `finish_reason`, `tool_calls_summary`, `input_preview`, `output_preview`, plus per-request `conversation_id` / `session_id` / `user_id` if set via `logger.context(...)`.

Per tool call: `tool_name`, `args_preview`, `result_preview`, `latency_ms`, status + error fields, linked to parent inference via `parent_inference_request_id`.

## PII redaction

Runs **inside your process** before any event leaves over the network. Default recognizers: email, phone (E.164 + NA), credit card (Luhn-validated), US SSN, IBAN, IPv4, AWS access keys. Configurable via `InferenceLogger(pii_redact=True, pii_recognizers=[...])`.

Zero third-party deps for redaction (pure regex). Server-side PII can run as defense-in-depth if your ingestion service supports it.

## Provider matrix

| Library | Integration | Pattern |
|---|---|---|
| `openai` (AsyncOpenAI) | `chatbot_sdk.integrations.openai.instrument(client, logger=...)` | monkey-patch |
| `anthropic` (AsyncAnthropic) | `chatbot_sdk.integrations.anthropic.instrument(client, logger=...)` | monkey-patch |
| `google-genai` (AsyncClient) | `chatbot_sdk.integrations.google.instrument(client, logger=...)` | monkey-patch |
| `langchain-core` | `chatbot_sdk.integrations.langchain.SDKCallback(sdk=logger)` | callback handler |
| `llama-index-core` | `chatbot_sdk.integrations.llamaindex.LlamaIndexCallback(logger)` | callback handler |
| Anything else | `async with logger.inference(...) as span:` | manual context manager |

## Per-request context

Use `logger.context(...)` to propagate per-request IDs through nested LLM/tool calls without threading them by hand:

```python
async with logger.context(conversation_id=conv_id, user_id=user_id):
    resp = await client.chat.completions.create(...)   # auto-traced, IDs stamped on the log
```

## Links

- Source: https://github.com/itsdun1/inferspect
- Issues: https://github.com/itsdun1/inferspect/issues
- License: MIT
