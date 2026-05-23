"""Direct OpenAI / Anthropic chat handlers — no LangChain in the path.

Owns the SDK-instrumented provider clients via a ``RawProviders`` factory
that's constructed once at startup (chat_service.main:lifespan). Every
``client.chat.completions.create`` / ``client.messages.create`` call goes
through the SDK's monkey-patch installed by ``instrument()`` at boot.

Why a factory class (vs the previous module-level globals): explicit
lifecycle, no module-state mutation, testable in isolation, multiple
instances possible (e.g. two loggers shipping to different ingestion
endpoints). Same idempotency story since ``wrap_method`` on the SDK side
tags wrapped methods with a sentinel — instrumenting the same client
twice is still a no-op.

Model dispatch (caller-side):
    "raw-openai/gpt-4o"                       → AsyncOpenAI.chat.completions.create
    "raw-anthropic/claude-haiku-4-5-20251001" → AsyncAnthropic.messages.create
Anything else stays on the LangChain path with the ``SDKCallback``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from anthropic import AsyncAnthropic
    from openai import AsyncOpenAI

    from chatbot_sdk import InferenceLogger

log = logging.getLogger(__name__)


@dataclass
class RawResponse:
    content: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int


# ─── Factory ─────────────────────────────────────────────────────────


class RawProviders:
    """Owns the SDK-instrumented OpenAI + Anthropic clients for the process.

    Construct ONCE at boot inside chat_service.main:lifespan. Pass into
    ``ChatService(raw_providers=...)``. If a provider library is missing or
    the API key is unset, the corresponding client stays None and raw calls
    raise a clear error.
    """

    def __init__(self, *, logger: "InferenceLogger | None") -> None:
        self._logger = logger
        self.openai: "AsyncOpenAI | None" = self._make_openai(logger)
        self.anthropic: "AsyncAnthropic | None" = self._make_anthropic(logger)

    @staticmethod
    def _make_openai(logger: "InferenceLogger | None") -> "AsyncOpenAI | None":
        try:
            from openai import AsyncOpenAI

            from chatbot_sdk.integrations.openai import instrument as instr_openai
        except Exception as exc:  # noqa: BLE001
            log.warning("RawProviders: OpenAI import failed: %s", exc)
            return None
        try:
            client = AsyncOpenAI()
        except Exception as exc:  # noqa: BLE001
            log.warning("RawProviders: AsyncOpenAI() init failed: %s", exc)
            return None
        if logger is not None:
            instr_openai(client, logger=logger)
        return client

    @staticmethod
    def _make_anthropic(logger: "InferenceLogger | None") -> "AsyncAnthropic | None":
        try:
            from anthropic import AsyncAnthropic

            from chatbot_sdk.integrations.anthropic import instrument as instr_anthropic
        except Exception as exc:  # noqa: BLE001
            log.warning("RawProviders: Anthropic import failed: %s", exc)
            return None
        try:
            client = AsyncAnthropic()
        except Exception as exc:  # noqa: BLE001
            log.warning("RawProviders: AsyncAnthropic() init failed: %s", exc)
            return None
        if logger is not None:
            instr_anthropic(client, logger=logger)
        return client

    # ─── Non-streaming dispatch ──────────────────────────────────────
    async def chat(
        self,
        *,
        model: str,
        system_prompt: str,
        history: list[tuple[str, str]],
        conversation_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
    ) -> RawResponse:
        actual_model = _strip_raw_prefix(model)
        ctx = self._ctx(conversation_id=conversation_id, user_id=user_id)

        if model.startswith("raw-openai/"):
            if self.openai is None:
                raise RuntimeError(
                    "OpenAI client not available — set OPENAI_API_KEY and restart chat-service"
                )
            async with ctx:
                resp = await self.openai.chat.completions.create(
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
            if self.anthropic is None:
                raise RuntimeError(
                    "Anthropic client not available — set ANTHROPIC_API_KEY and restart chat-service"
                )
            async with ctx:
                resp = await self.anthropic.messages.create(
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

    # ─── Streaming dispatch ──────────────────────────────────────────
    async def stream_chat(
        self,
        *,
        model: str,
        system_prompt: str,
        history: list[tuple[str, str]],
        conversation_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
    ) -> AsyncIterator[str]:
        """Yield text deltas as the provider stream emits them."""
        actual_model = _strip_raw_prefix(model)
        ctx = self._ctx(conversation_id=conversation_id, user_id=user_id)

        if model.startswith("raw-openai/"):
            if self.openai is None:
                raise RuntimeError(
                    "OpenAI client not available — set OPENAI_API_KEY and restart chat-service"
                )
            async with ctx:
                stream = await self.openai.chat.completions.create(
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
            if self.anthropic is None:
                raise RuntimeError(
                    "Anthropic client not available — set ANTHROPIC_API_KEY and restart chat-service"
                )
            async with ctx:
                stream = await self.anthropic.messages.create(
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

    # ─── Internals ───────────────────────────────────────────────────
    def _ctx(
        self,
        *,
        conversation_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
    ) -> Any:
        """SDK context manager that stamps per-request ids on the span via
        ContextVar. No-op when no SDK logger is wired up."""
        if self._logger is None:
            return nullcontext()
        return self._logger.context(conversation_id=conversation_id, user_id=user_id)


# ─── Module-level helpers (pure functions, no state) ─────────────────


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
        return model[len("raw-openai/") :]
    if model.startswith("raw-anthropic/"):
        return model[len("raw-anthropic/") :]
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
    history: list[tuple[str, str]],
) -> list[dict[str, str]]:
    """Anthropic doesn't accept 'system' role in the messages array — only
    user/assistant. The system prompt is passed as a top-level ``system``
    kwarg by the caller."""
    msgs: list[dict[str, str]] = []
    for role, content in history:
        if role in {"user", "assistant"}:
            msgs.append({"role": role, "content": content})
    return msgs
