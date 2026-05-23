"""Optional OpenAI integration. Install with ``pip install chatbot-sdk[openai]``.

After ``instrument(client, logger=logger)``, every call to
``client.chat.completions.create(...)`` (sync via ``OpenAI`` is not supported
in v1 — see below) automatically opens an inference span, captures the
response, and ships a log.

Sync clients
~~~~~~~~~~~~
v1 only supports ``AsyncOpenAI``. For sync code (Flask, Django sync,
notebooks) use :class:`chatbot_sdk.SyncInferenceLogger` together with the
manual ``with logger.inference(...)`` pattern — see
``examples/sync_flask.py``. We deliberately avoid driving the async daemon
loop from inside a sync customer method to keep error surfaces predictable.
"""

# ruff: noqa: E402 — vendor imports must follow _require_extra() gate.

from __future__ import annotations

from chatbot_sdk._require import _require_extra

_require_extra("openai", "openai")

import inspect
import json
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from chatbot_sdk.client import InferenceLogger
from chatbot_sdk.integrations._instrument import current_ctx_kwargs, wrap_method


def instrument(client: Any, *, logger: InferenceLogger) -> None:
    """Monkey-patch ``client.chat.completions.create`` so every call is auto-traced.

    Idempotent — re-instrumenting the same client is a no-op."""

    # OpenAI's AsyncOpenAI doesn't decorate ``.create`` with ``async def`` (it's
    # a regular method that returns awaitables), so ``inspect.iscoroutinefunction``
    # always returns False. Fall back to sniffing the client class name.
    is_async = type(client).__name__.startswith("Async")

    def _factory(original: Any) -> Any:
        if is_async or inspect.iscoroutinefunction(original):
            return _build_async_wrapper(original, logger)
        return _build_sync_stub(original)

    wrap_method(client, "chat.completions.create", _factory)


def _build_sync_stub(original: Any) -> Any:
    """Sync clients are out of scope for v1; raise with a clear pointer."""

    def _stub(*args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "chatbot_sdk.integrations.openai.instrument() only supports "
            "AsyncOpenAI in v1. For sync code, use SyncInferenceLogger and "
            "wrap calls manually: "
            "`with logger.inference(...) as span: resp = client.chat.completions.create(...); span.set_response(resp)`."
        )

    return _stub


def _build_async_wrapper(original: Any, logger: InferenceLogger) -> Any:
    """Wrap an async ``create(...)`` method so each call is traced.

    Handles both the non-stream path (await once, capture response) and the
    stream=True path (return an async iterator that observes each chunk)."""

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        # Measure how much time the SDK adds vs the actual API call. Pre-call
        # time = wrapper setup + span construction (we stamp it as metadata so
        # _finalize can sum with its own time). api_call_ms = pure HTTP+server.
        t_start = time.perf_counter()

        model = kwargs.get("model", "unknown")
        stream = bool(kwargs.get("stream", False))
        input_preview = _last_user_text(kwargs.get("messages") or [])

        cm = logger.inference(
            provider="openai",
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

        if stream:
            # Stream path — close the span when the customer drains the iterator.
            # api_call_ms is updated again inside _stream_proxy at drain time.
            span.set_metadata(
                sdk_pre_call_ms=round((t_pre_call - t_start) * 1000, 3),
                api_call_ms=round((t_post_call - t_pre_call) * 1000, 3),
            )
            return _stream_proxy(result, span, cm, t_start, t_pre_call)

        # Non-stream path — record timing + capture once + close.
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


async def _stream_proxy(
    stream: Any, span: Any, cm: Any, t_start: float, t_pre_call: float
) -> AsyncIterator[Any]:
    """Yield chunks while observing each one onto the span; close on drain.

    Updates ``api_call_ms`` on drain so it reflects the FULL stream duration
    (not just time to first chunk) and ``sdk_pre_call_ms`` stays as the
    pre-API setup cost."""
    try:
        async for chunk in stream:
            try:
                span.observe_chunk(_normalize_chunk(chunk))
            except Exception:  # noqa: BLE001 — never crash the user's stream
                pass
            yield chunk
    except BaseException as exc:
        await cm.__aexit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        # Stream fully drained — update api_call_ms to cover the whole stream.
        t_end = time.perf_counter()
        span.set_metadata(
            sdk_pre_call_ms=round((t_pre_call - t_start) * 1000, 3),
            api_call_ms=round((t_end - t_pre_call) * 1000, 3),
        )
        await cm.__aexit__(None, None, None)


def _normalize_response(response: Any) -> SimpleNamespace:
    """Pack an OpenAI ChatCompletion into the shape InferenceSpan.set_response wants."""
    content = ""
    tool_calls: list[dict[str, Any]] = []
    try:
        choice = response.choices[0]
        msg = getattr(choice, "message", None)
        content = getattr(msg, "content", "") or ""
        raw_tcs = getattr(msg, "tool_calls", None) or []
        for tc in raw_tcs:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", None) if fn is not None else None
            raw_args = getattr(fn, "arguments", None) if fn is not None else None
            args: Any
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except (ValueError, TypeError):
                    args = raw_args
            else:
                args = raw_args or {}
            tool_calls.append({"name": name or "", "args": args})
    except (AttributeError, IndexError, TypeError):
        pass

    usage = getattr(response, "usage", None)
    usage_metadata: dict[str, int] | None = None
    if usage is not None:
        usage_metadata = {
            "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
        }

    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        usage_metadata=usage_metadata,
    )


def _normalize_chunk(chunk: Any) -> SimpleNamespace:
    """Pack an OpenAI ChatCompletionChunk into observe_chunk's expected shape.

    When ``stream_options={"include_usage": True}`` is set on the original
    call, OpenAI sends a FINAL chunk with empty ``choices`` and a populated
    ``usage``. We extract it here so InferenceSpan can record the totals."""
    content = ""
    tool_call_chunks: list[dict[str, Any]] = []
    try:
        delta = chunk.choices[0].delta
        content = getattr(delta, "content", "") or ""
        raw_tcs = getattr(delta, "tool_calls", None) or []
        for tc in raw_tcs:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", None) if fn is not None else None
            raw_args = getattr(fn, "arguments", None) if fn is not None else None
            if name or raw_args:
                tool_call_chunks.append({"name": name or "", "args": raw_args or ""})
    except (AttributeError, IndexError, TypeError):
        pass

    # Token usage — usually only on the FINAL chunk when include_usage is on.
    usage = getattr(chunk, "usage", None)
    usage_metadata: dict[str, int] | None = None
    if usage is not None:
        usage_metadata = {
            "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
        }

    return SimpleNamespace(
        content=content,
        tool_call_chunks=tool_call_chunks,
        usage_metadata=usage_metadata,
    )


def _last_user_text(messages: list[Any]) -> str:
    """Pluck the last user message text, truncated to 500 chars."""
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
