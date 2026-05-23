"""Optional LlamaIndex integration. Install with ``pip install chatbot-sdk[llamaindex]``.

``LlamaIndexCallback`` plugs into ``llama_index.core.callbacks`` and emits SDK
inference + tool spans for every LLM call / function call routed through
LlamaIndex. Use it with ``set_global_handler`` or by passing into the
``CallbackManager`` directly.
"""

# ruff: noqa: E402 — vendor imports must follow _require_extra() gate.

from __future__ import annotations

from chatbot_sdk._require import _require_extra

_require_extra("llamaindex", "llama_index.core")

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from llama_index.core.callbacks.base_handler import BaseCallbackHandler
from llama_index.core.callbacks.schema import CBEventType

from chatbot_sdk.client import InferenceLogger, InferenceSpan, ToolSpan, current_context


class LlamaIndexCallback(BaseCallbackHandler):
    """Routes LlamaIndex callback events to the SDK as inference/tool spans."""

    def __init__(self, sdk: InferenceLogger) -> None:
        super().__init__(event_starts_to_ignore=[], event_ends_to_ignore=[])
        self._sdk = sdk
        self._inference_spans: dict[str, InferenceSpan] = {}
        self._inference_cms: dict[str, Any] = {}
        self._tool_spans: dict[str, ToolSpan] = {}
        self._tool_cms: dict[str, Any] = {}
        self.last_inference_request_id: Any | None = None

    # ─── LlamaIndex callback API ─────────────────────────────────
    def start_trace(self, trace_id: str | None = None) -> None:
        return None

    def end_trace(
        self,
        trace_id: str | None = None,
        trace_map: dict[str, list[str]] | None = None,
    ) -> None:
        return None

    def on_event_start(
        self,
        event_type: CBEventType,
        payload: dict[Any, Any] | None = None,
        event_id: str = "",
        parent_id: str = "",
        **kwargs: Any,
    ) -> str:
        payload = payload or {}
        if event_type == CBEventType.LLM:
            self._open_inference(event_id, payload)
        elif event_type == CBEventType.FUNCTION_CALL:
            self._open_tool(event_id, payload)
        return event_id

    def on_event_end(
        self,
        event_type: CBEventType,
        payload: dict[Any, Any] | None = None,
        event_id: str = "",
        **kwargs: Any,
    ) -> None:
        payload = payload or {}
        if event_type == CBEventType.LLM:
            self._close_inference(event_id, payload)
        elif event_type == CBEventType.FUNCTION_CALL:
            self._close_tool(event_id, payload)

    # ─── helpers ─────────────────────────────────────────────────
    def _ids(self) -> dict[str, Any]:
        ctx = current_context()
        out: dict[str, Any] = {}
        for key in ("conversation_id", "session_id", "user_id"):
            v = ctx.get(key)
            if v is not None:
                out[key] = v
        return out

    def _open_inference(self, event_id: str, payload: dict[Any, Any]) -> None:
        model = _model_from_payload(payload)
        provider = _provider_for(model)
        cm = self._sdk.inference(
            provider=provider,
            model=model or "unknown",
            stream=False,
            input_preview=_input_preview(payload),
            **self._ids(),
        )
        span = _run(cm.__aenter__())
        self._inference_cms[event_id] = cm
        self._inference_spans[event_id] = span

    def _close_inference(self, event_id: str, payload: dict[Any, Any]) -> None:
        span = self._inference_spans.pop(event_id, None)
        cm = self._inference_cms.pop(event_id, None)
        if span is None or cm is None:
            return
        try:
            span.set_response(_normalize_llm_response(payload))
            self.last_inference_request_id = span.request_id
        finally:
            _run(cm.__aexit__(None, None, None))

    def _open_tool(self, event_id: str, payload: dict[Any, Any]) -> None:
        tool_name = _tool_name_from_payload(payload)
        args_preview = _tool_args_preview(payload)
        cm = self._sdk.tool_call(
            tool_name=tool_name,
            parent_inference_request_id=self.last_inference_request_id,
            args_preview=args_preview,
            **self._ids(),
        )
        span = _run(cm.__aenter__())
        self._tool_cms[event_id] = cm
        self._tool_spans[event_id] = span

    def _close_tool(self, event_id: str, payload: dict[Any, Any]) -> None:
        span = self._tool_spans.pop(event_id, None)
        cm = self._tool_cms.pop(event_id, None)
        if span is None or cm is None:
            return
        try:
            span.set_result(payload.get("function_call_response") or payload.get("response") or "")
        finally:
            _run(cm.__aexit__(None, None, None))


# ─── module-level helpers ───────────────────────────────────────
def _run(awaitable: Any) -> Any:
    """Drive an awaitable from sync callback context.

    LlamaIndex callbacks are sync entry points. When we're already inside an
    event loop (the common case for async pipelines), ``asyncio.run`` would
    raise; in that case we schedule the coroutine on the running loop and
    block briefly. When no loop is running we ``asyncio.run`` directly.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    # We're inside a running loop — drive the coro to completion by stepping
    # it manually. The SDK's inference/tool_call ctxmgrs only suspend on
    # transport submit (a sync call), so a single .send() is enough.
    coro = awaitable
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    except Exception:
        raise
    finally:
        coro.close()
    # If somehow it suspended (shouldn't happen on the SDK path), fall back
    # to scheduling and waiting briefly. Importing ``concurrent.futures`` only
    # if needed keeps the cold path tight.
    import concurrent.futures

    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return fut.result(timeout=5.0)
    except concurrent.futures.TimeoutError:
        return None


def _provider_for(model_name: str | None) -> str:
    if not model_name:
        return "unknown"
    lower = model_name.lower()
    if lower.startswith(("gemini", "google")):
        return "google"
    if lower.startswith(("gpt", "o1", "openai")):
        return "openai"
    if lower.startswith(("claude", "anthropic")):
        return "anthropic"
    return "unknown"


def _model_from_payload(payload: dict[Any, Any]) -> str | None:
    """LlamaIndex stuffs the model name in a few possible keys."""
    for key in ("model_name", "model", "serialized"):
        v = payload.get(key)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, dict):
            for sub in ("model", "model_name"):
                if v.get(sub):
                    return str(v[sub])
    # Sometimes nested under ``additional_kwargs`` on the request.
    return None


def _input_preview(payload: dict[Any, Any]) -> str:
    msgs = payload.get("messages")
    if msgs:
        try:
            last = msgs[-1]
            content = getattr(last, "content", None) or (
                last.get("content") if isinstance(last, dict) else ""
            )
            return str(content)[:500]
        except (AttributeError, IndexError, TypeError):
            pass
    prompt = payload.get("prompt") or payload.get("formatted_prompt")
    if isinstance(prompt, str):
        return prompt[:500]
    return ""


def _normalize_llm_response(payload: dict[Any, Any]) -> SimpleNamespace:
    response = payload.get("response")
    text = ""
    if response is not None:
        # ChatResponse-like: ``response.message.content``.
        msg = getattr(response, "message", None)
        if msg is not None:
            text = str(getattr(msg, "content", "") or "")
        if not text:
            text = str(getattr(response, "text", "") or "")
    usage_metadata: dict[str, int] | None = None
    raw = getattr(response, "raw", None) if response is not None else None
    usage = getattr(raw, "usage", None) if raw is not None else None
    if usage is not None:
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        usage_metadata = {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    return SimpleNamespace(content=text, tool_calls=[], usage_metadata=usage_metadata)


def _tool_name_from_payload(payload: dict[Any, Any]) -> str:
    tool = payload.get("tool")
    if tool is not None:
        name = getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None)
        if name:
            return str(name)
    return str(payload.get("function_call_name") or "unknown_tool")


def _tool_args_preview(payload: dict[Any, Any]) -> str:
    args = payload.get("function_call") or payload.get("arguments") or payload.get("input")
    if args is None:
        return ""
    if isinstance(args, str):
        return args[:500]
    try:
        return json.dumps(args, default=str)[:500]
    except (TypeError, ValueError):
        return str(args)[:500]
