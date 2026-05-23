"""Optional Google GenAI integration. Install with ``pip install chatbot-sdk[google]``.

After ``instrument(client, logger=logger)``:
  - ``client.aio.models.generate_content(...)`` is auto-traced
  - ``client.aio.models.generate_content_stream(...)`` is auto-traced
  - ``client.models.generate_content(...)`` raises ``NotImplementedError`` (use
    ``SyncInferenceLogger`` with manual span wrapping for sync code)
"""

# ruff: noqa: E402 — vendor imports must follow _require_extra() gate.

from __future__ import annotations

from chatbot_sdk._require import _require_extra

_require_extra("google", "google.genai")

import inspect
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from chatbot_sdk.client import InferenceLogger
from chatbot_sdk.integrations._instrument import current_ctx_kwargs, wrap_method


def instrument(client: Any, *, logger: InferenceLogger) -> None:
    """Monkey-patch the google-genai client. Idempotent."""

    def _async_factory(original: Any) -> Any:
        if inspect.iscoroutinefunction(original) or inspect.isasyncgenfunction(original):
            return _build_async_wrapper(original, logger, streaming=False)
        return _build_sync_stub()

    def _async_stream_factory(original: Any) -> Any:
        if inspect.iscoroutinefunction(original) or inspect.isasyncgenfunction(original):
            return _build_async_wrapper(original, logger, streaming=True)
        return _build_sync_stub()

    def _sync_stub_factory(_original: Any) -> Any:
        return _build_sync_stub()

    # Async paths (the supported ones).
    if hasattr(client, "aio") and hasattr(client.aio, "models"):
        wrap_method(client, "aio.models.generate_content", _async_factory)
        wrap_method(client, "aio.models.generate_content_stream", _async_stream_factory)
    # Sync path — leave a friendly stub.
    if hasattr(client, "models") and hasattr(client.models, "generate_content"):
        wrap_method(client, "models.generate_content", _sync_stub_factory)


def _build_sync_stub() -> Any:
    def _stub(*args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "chatbot_sdk.integrations.google.instrument() only supports the "
            "async ``client.aio.models.*`` paths in v1. For sync code use "
            "SyncInferenceLogger with `with logger.inference(...)` manually."
        )

    return _stub


def _build_async_wrapper(original: Any, logger: InferenceLogger, *, streaming: bool) -> Any:
    if streaming:

        async def stream_wrapper(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            model = kwargs.get("model", "unknown")
            input_preview = _input_preview(kwargs)
            cm = logger.inference(
                provider="google",
                model=model,
                stream=True,
                input_preview=input_preview,
                **current_ctx_kwargs(),
            )
            span = await cm.__aenter__()
            final_chunk: Any = None
            try:
                stream = original(*args, **kwargs)
                # ``generate_content_stream`` returns an awaitable that resolves
                # to an async iterator; if it's already an async-gen, awaiting
                # is unnecessary.
                if inspect.iscoroutine(stream):
                    stream = await stream
                async for chunk in stream:
                    try:
                        span.observe_chunk(_normalize_chunk(chunk))
                    except Exception:  # noqa: BLE001
                        pass
                    final_chunk = chunk
                    yield chunk
            except BaseException as exc:
                await cm.__aexit__(type(exc), exc, exc.__traceback__)
                raise
            else:
                # Use the last chunk's usage metadata for final token counts.
                if final_chunk is not None:
                    try:
                        span.set_response(_normalize_response(final_chunk))
                    except Exception:  # noqa: BLE001
                        pass
                await cm.__aexit__(None, None, None)

        return stream_wrapper

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model", "unknown")
        input_preview = _input_preview(kwargs)
        cm = logger.inference(
            provider="google",
            model=model,
            stream=False,
            input_preview=input_preview,
            **current_ctx_kwargs(),
        )
        span = await cm.__aenter__()
        try:
            result = await original(*args, **kwargs)
        except BaseException as exc:
            await cm.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        try:
            span.set_response(_normalize_response(result))
        finally:
            await cm.__aexit__(None, None, None)
        return result

    return wrapper


def _input_preview(kwargs: dict[str, Any]) -> str:
    """Try ``contents=`` (the most common arg). Truncate to 500."""
    contents = kwargs.get("contents")
    if contents is None:
        return ""
    if isinstance(contents, str):
        return contents[:500]
    if isinstance(contents, list):
        parts: list[str] = []
        for c in contents:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                # google.genai Content dicts look like {"role": ..., "parts": [{"text": ...}]}.
                for p in c.get("parts", []) or []:
                    if isinstance(p, dict) and "text" in p:
                        parts.append(str(p["text"]))
        return "".join(parts)[:500]
    return str(contents)[:500]


def _normalize_response(response: Any) -> SimpleNamespace:
    text = ""
    try:
        text = getattr(response, "text", None) or ""
    except (AttributeError, ValueError):
        text = ""
    if not text:
        # Fall back to walking candidates[0].content.parts[].text.
        try:
            cand = response.candidates[0]
            for p in cand.content.parts:
                t = getattr(p, "text", None)
                if t:
                    text += t
        except (AttributeError, IndexError, TypeError):
            pass

    usage_metadata: dict[str, int] | None = None
    usage = getattr(response, "usage_metadata", None)
    if usage is not None:
        input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        usage_metadata = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    return SimpleNamespace(content=text, tool_calls=[], usage_metadata=usage_metadata)


def _normalize_chunk(chunk: Any) -> SimpleNamespace:
    text = ""
    try:
        text = getattr(chunk, "text", None) or ""
    except (AttributeError, ValueError):
        text = ""
    return SimpleNamespace(content=text, tool_call_chunks=None, usage_metadata=None)
