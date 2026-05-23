"""Direct OpenAI / Anthropic chat handlers — no LangChain in the path.

Customer demonstration of the "third-party uses inferspect-sdk via raw provider
clients + auto-instrumentation" workflow. Same SDK, same logs in ClickHouse,
just a different code path: `instrument()` monkey-patches the vendor client
instead of the LangChain `SDKCallback` riding the agent's callback bus.

Model dispatch
--------------
The chat service routes by the ``model`` string sent from the UI:

    "raw-openai/gpt-4o"                       → AsyncOpenAI.chat.completions.create
    "raw-anthropic/claude-haiku-4-5-20251001" → AsyncAnthropic.messages.create

Anything else (``gpt-4o``, ``claude-*``, ``gemini-*``) stays on the existing
LangChain path with the ``SDKCallback``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from chatbot_sdk import InferenceLogger

log = logging.getLogger(__name__)


@dataclass
class RawResponse:
    content: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int


# Module-level singletons: build the provider clients once, monkey-patch with
# instrument() once, reuse for the lifetime of the process.
_openai_client: Any | None = None
_anthropic_client: Any | None = None
_instrumented = False


def _setup(sdk_logger: "InferenceLogger | None") -> None:
    """Build the two raw provider clients and instrument them with the SDK.

    Idempotent. If a provider library or key is missing we silently leave the
    client at None — calls to that path then 503 with a helpful error."""
    global _openai_client, _anthropic_client, _instrumented
    if _instrumented:
        return
    _instrumented = True

    if sdk_logger is None:
        log.warning("raw_providers: SDK logger is None — raw paths will not emit logs")

    try:
        from openai import AsyncOpenAI

        from chatbot_sdk.integrations.openai import instrument as instr_openai

        _openai_client = AsyncOpenAI()
        if sdk_logger is not None:
            instr_openai(_openai_client, logger=sdk_logger)
    except Exception as exc:  # noqa: BLE001
        log.warning("raw_providers: OpenAI setup failed: %s", exc)
        _openai_client = None

    try:
        from anthropic import AsyncAnthropic

        from chatbot_sdk.integrations.anthropic import instrument as instr_anthropic

        _anthropic_client = AsyncAnthropic()
        if sdk_logger is not None:
            instr_anthropic(_anthropic_client, logger=sdk_logger)
    except Exception as exc:  # noqa: BLE001
        log.warning("raw_providers: Anthropic setup failed: %s", exc)
        _anthropic_client = None


def is_raw_model(model: str) -> bool:
    return model.startswith("raw-openai/") or model.startswith("raw-anthropic/")


def provider_for_raw(model: str) -> str:
    if model.startswith("raw-openai/"):
        return "openai"
    if model.startswith("raw-anthropic/"):
        return "anthropic"
    return "unknown"


def _strip_raw_prefix(model: str) -> str:
    if model.startswith("raw-openai/"):
        return model[len("raw-openai/"):]
    if model.startswith("raw-anthropic/"):
        return model[len("raw-anthropic/"):]
    return model


def _to_openai_messages(
    system_prompt: str, history: list[tuple[str, str]]
) -> list[dict[str, str]]:
    msgs: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for role, content in history:
        if role in {"user", "assistant", "system"}:
            msgs.append({"role": role, "content": content})
    return msgs


def _to_anthropic_messages(
    history: list[tuple[str, str]]
) -> list[dict[str, str]]:
    """Anthropic doesn't accept 'system' role in the messages array — only
    user/assistant. The system prompt is passed as a top-level ``system``
    kwarg by the caller."""
    msgs: list[dict[str, str]] = []
    for role, content in history:
        if role in {"user", "assistant"}:
            msgs.append({"role": role, "content": content})
    return msgs


def _logger_ctx(
    sdk_logger: "InferenceLogger | None",
    *,
    conversation_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
) -> Any:
    """Yield the SDK context manager (sets conversation_id/user_id ContextVars
    so the auto-instrumentation can stamp them on the span). If no SDK logger
    is wired up, return a no-op nullcontext."""
    if sdk_logger is None:
        return nullcontext()
    return sdk_logger.context(conversation_id=conversation_id, user_id=user_id)


# ─── Non-streaming ──────────────────────────────────────────────────


async def chat_raw(
    *,
    model: str,
    system_prompt: str,
    history: list[tuple[str, str]],
    conversation_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    sdk_logger: "InferenceLogger | None",
) -> RawResponse:
    """Non-streaming dispatch for a raw-* model."""
    _setup(sdk_logger)

    actual_model = _strip_raw_prefix(model)
    ctx = _logger_ctx(sdk_logger, conversation_id=conversation_id, user_id=user_id)

    if model.startswith("raw-openai/"):
        if _openai_client is None:
            raise RuntimeError(
                "OpenAI client not available — set OPENAI_API_KEY and restart chat-service"
            )
        async with ctx:
            resp = await _openai_client.chat.completions.create(
                model=actual_model,
                messages=_to_openai_messages(system_prompt, history),
            )
        content = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        return RawResponse(
            content=content,
            provider="openai",
            model=actual_model,
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        )

    if model.startswith("raw-anthropic/"):
        if _anthropic_client is None:
            raise RuntimeError(
                "Anthropic client not available — set ANTHROPIC_API_KEY and restart chat-service"
            )
        async with ctx:
            resp = await _anthropic_client.messages.create(
                model=actual_model,
                max_tokens=4096,
                system=system_prompt,
                messages=_to_anthropic_messages(history),
            )
        text_parts: list[str] = []
        for block in getattr(resp, "content", []) or []:
            if getattr(block, "type", None) == "text":
                text_parts.append(getattr(block, "text", "") or "")
        usage = getattr(resp, "usage", None)
        return RawResponse(
            content="".join(text_parts),
            provider="anthropic",
            model=actual_model,
            prompt_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )

    raise ValueError(f"unknown raw model: {model!r}")


# ─── Streaming ──────────────────────────────────────────────────────


async def stream_chat_raw(
    *,
    model: str,
    system_prompt: str,
    history: list[tuple[str, str]],
    conversation_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    sdk_logger: "InferenceLogger | None",
) -> AsyncIterator[str]:
    """Stream the assistant text in deltas. Yields plain text chunks; the
    caller wraps them into SSE frames."""
    _setup(sdk_logger)

    actual_model = _strip_raw_prefix(model)
    ctx = _logger_ctx(sdk_logger, conversation_id=conversation_id, user_id=user_id)

    if model.startswith("raw-openai/"):
        if _openai_client is None:
            raise RuntimeError(
                "OpenAI client not available — set OPENAI_API_KEY and restart chat-service"
            )
        async with ctx:
            stream = await _openai_client.chat.completions.create(
                model=actual_model,
                messages=_to_openai_messages(system_prompt, history),
                stream=True,
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                text = getattr(delta, "content", None)
                if text:
                    yield text
        return

    if model.startswith("raw-anthropic/"):
        if _anthropic_client is None:
            raise RuntimeError(
                "Anthropic client not available — set ANTHROPIC_API_KEY and restart chat-service"
            )
        async with ctx:
            # Anthropic's streaming yields semantic events; we extract text
            # from content_block_delta.text_delta events.
            stream = await _anthropic_client.messages.create(
                model=actual_model,
                max_tokens=4096,
                system=system_prompt,
                messages=_to_anthropic_messages(history),
                stream=True,
            )
            async for event in stream:
                etype = getattr(event, "type", None)
                if etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    text = getattr(delta, "text", None)
                    if text:
                        yield text
        return

    raise ValueError(f"unknown raw model: {model!r}")
