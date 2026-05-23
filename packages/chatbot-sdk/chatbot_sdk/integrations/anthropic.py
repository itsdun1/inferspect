"""Optional Anthropic integration. Install with ``pip install chatbot-sdk[anthropic]``.

After ``instrument(client, logger=logger)``, every call to
``client.messages.create(...)`` (``AsyncAnthropic``) is auto-traced. Sync
``Anthropic`` is out of scope for v1; use
:class:`chatbot_sdk.SyncInferenceLogger` with manual span wrapping instead.
"""

# ruff: noqa: E402 — vendor imports must follow _require_extra() gate.

from __future__ import annotations

from chatbot_sdk._require import _require_extra

_require_extra("anthropic", "anthropic")

import inspect
import time
from types import SimpleNamespace
from typing import Any

from chatbot_sdk.client import InferenceLogger
from chatbot_sdk.integrations._instrument import current_ctx_kwargs, wrap_method


def instrument(client: Any, *, logger: InferenceLogger) -> None:
    """Monkey-patch ``client.messages.create`` so every call is auto-traced.

    Idempotent — re-instrumenting the same client is a no-op."""

    # Anthropic's AsyncAnthropic exposes ``.create`` without an ``async def``
    # decoration — same issue as OpenAI. Sniff the client class name instead.
    is_async = type(client).__name__.startswith("Async")

    def _factory(original: Any) -> Any:
        if is_async or inspect.iscoroutinefunction(original):
            return _build_async_wrapper(original, logger)
        return _build_sync_stub(original)

    wrap_method(client, "messages.create", _factory)


def _build_sync_stub(original: Any) -> Any:
    def _stub(*args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "chatbot_sdk.integrations.anthropic.instrument() only supports "
            "AsyncAnthropic in v1. For sync code, use SyncInferenceLogger "
            "with `with logger.inference(...)` around the call."
        )

    return _stub


def _build_async_wrapper(original: Any, logger: InferenceLogger) -> Any:
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        t_start = time.perf_counter()

        model = kwargs.get("model", "unknown")
        stream = bool(kwargs.get("stream", False))
        input_preview = _last_user_text(kwargs.get("messages") or [])

        cm = logger.inference(
            provider="anthropic",
            model=model,
            stream=stream,
            input_preview=input_preview,
            **current_ctx_kwargs(),
        )
        span = await cm.__aenter__()
        t_pre_call = time.perf_counter()

        try:
            result = await original(*args, **kwargs)
        except BaseException as exc:
            await cm.__aexit__(type(exc), exc, exc.__traceback__)
            raise

        t_post_call = time.perf_counter()

        try:
            span.set_response(_normalize_response(result))
            span.set_metadata(
                sdk_pre_call_ms=round((t_pre_call - t_start) * 1000, 3),
                api_call_ms=round((t_post_call - t_pre_call) * 1000, 3),
            )
        finally:
            await cm.__aexit__(None, None, None)
        return result

    return wrapper


def _normalize_response(response: Any) -> SimpleNamespace:
    """Pack an Anthropic Message into InferenceSpan.set_response shape."""
    content = ""
    tool_calls: list[dict[str, Any]] = []
    raw_blocks = getattr(response, "content", None) or []
    for block in raw_blocks:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            content += getattr(block, "text", "") or ""
        elif block_type == "tool_use":
            tool_calls.append(
                {
                    "name": getattr(block, "name", "") or "",
                    "args": getattr(block, "input", {}) or {},
                }
            )

    usage = getattr(response, "usage", None)
    usage_metadata: dict[str, int] | None = None
    if usage is not None:
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        usage_metadata = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        usage_metadata=usage_metadata,
    )


def _last_user_text(messages: list[Any]) -> str:
    if not messages:
        return ""
    for m in reversed(messages):
        if not isinstance(m, dict):
            continue
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                return content[:500]
            if isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict) and "text" in p:
                        parts.append(str(p["text"]))
                    elif isinstance(p, str):
                        parts.append(p)
                return "".join(parts)[:500]
    return ""
