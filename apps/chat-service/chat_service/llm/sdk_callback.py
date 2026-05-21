"""LangChain async callback handler that emits SDK spans for every LLM call
and every tool dispatch inside a LangGraph agent invocation.

Why a callback instead of wrapping the whole agent in one span: the agent
may make multiple LLM calls in a single user turn (think → call tool → think
again). We want one ``inference`` log per LLM call and one ``tool_execution``
log per tool dispatch, all linked back to the parent conversation/session.

Lifecycle (per agent invocation):
  on_chat_model_start  → open InferenceSpan, stash by run_id
  on_chat_model_stream → observe_chunk on the matching span (ttft + tokens)
  on_chat_model_end    → close the span (commits the log to the SDK queue)
  on_tool_start        → open ToolSpan, stash by run_id
  on_tool_end          → close the tool span with the result
  on_chain_*           → ignored (LangGraph graph-level events, not LLM calls)

Errors fall through ``on_*_error`` paths which close the spans with status
``error``.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler

from chatbot_sdk.client import InferenceLogger, InferenceSpan, ToolSpan
from chat_service.llm.langchain_adapter import provider_for

logger = logging.getLogger(__name__)


class SDKCallback(AsyncCallbackHandler):
    """Routes LangChain run events to the chatbot SDK as inference/tool logs."""

    def __init__(
        self,
        *,
        sdk: InferenceLogger,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID | None,
        session_id: uuid.UUID | None = None,
        model_hint: str | None = None,
    ) -> None:
        self._sdk = sdk
        self._conv = conversation_id
        self._user = user_id
        self._sess = session_id
        self._model_hint = model_hint
        # Per-run state.
        self._inference_spans: dict[uuid.UUID, InferenceSpan] = {}
        self._inference_cms: dict[uuid.UUID, Any] = {}
        self._tool_spans: dict[uuid.UUID, ToolSpan] = {}
        self._tool_cms: dict[uuid.UUID, Any] = {}
        # If you want to expose request_ids upstream (e.g. for the assistant
        # message's ``inference_request_id`` column), keep the last one.
        self.last_inference_request_id: uuid.UUID | None = None
        self.tool_calls_made: list[dict[str, Any]] = []

    # ─── LLM calls ───────────────────────────────────────────────
    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list],
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        model = _model_from(serialized, metadata, self._model_hint)
        provider = provider_for(model) if model else "unknown"
        input_preview = _last_human_text(messages)

        cm = self._sdk.inference(
            provider=provider,
            model=model or "unknown",
            conversation_id=self._conv,
            session_id=self._sess,
            user_id=self._user,
            stream=True,
            input_preview=input_preview,
            metadata={"langgraph_run_id": str(run_id)},
        )
        span = await cm.__aenter__()
        self._inference_cms[run_id] = cm
        self._inference_spans[run_id] = span

    async def on_llm_new_token(
        self,
        token: str,
        *,
        chunk: Any | None = None,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        span = self._inference_spans.get(run_id)
        if span is None:
            return
        if chunk is not None:
            span.observe_chunk(chunk)
        elif token:
            # Fallback for callbacks that don't pass the chunk object.
            span.observe_chunk(type("_T", (), {"content": token, "usage_metadata": None, "tool_call_chunks": None})())

    async def on_llm_end(
        self,
        response: Any,
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        span = self._inference_spans.pop(run_id, None)
        cm = self._inference_cms.pop(run_id, None)
        if span is None or cm is None:
            return
        # Extract the final AIMessage if present.
        final_msg = _final_ai_message(response)
        if final_msg is not None:
            span.set_response(final_msg)
            tool_calls = getattr(final_msg, "tool_calls", None) or []
            for tc in tool_calls:
                self.tool_calls_made.append({"name": tc.get("name"), "args": tc.get("args")})
        self.last_inference_request_id = span.request_id
        await cm.__aexit__(None, None, None)

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        cm = self._inference_cms.pop(run_id, None)
        self._inference_spans.pop(run_id, None)
        if cm is not None:
            await cm.__aexit__(type(error), error, error.__traceback__)

    # ─── Tools ───────────────────────────────────────────────────
    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        tool_name = (serialized or {}).get("name") or "unknown_tool"
        args_preview = json.dumps(inputs, default=str) if inputs else (input_str or "")
        cm = self._sdk.tool_call(
            tool_name=tool_name,
            parent_inference_request_id=self.last_inference_request_id,
            conversation_id=self._conv,
            session_id=self._sess,
            user_id=self._user,
            args_preview=args_preview,
            metadata={"langgraph_run_id": str(run_id)},
        )
        span = await cm.__aenter__()
        self._tool_cms[run_id] = cm
        self._tool_spans[run_id] = span

    async def on_tool_end(
        self,
        output: Any,
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        span = self._tool_spans.pop(run_id, None)
        cm = self._tool_cms.pop(run_id, None)
        if span is None or cm is None:
            return
        span.set_result(output)
        await cm.__aexit__(None, None, None)

    async def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        cm = self._tool_cms.pop(run_id, None)
        self._tool_spans.pop(run_id, None)
        if cm is not None:
            await cm.__aexit__(type(error), error, error.__traceback__)


# ─── helpers ─────────────────────────────────────────────────────
def _model_from(
    serialized: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    hint: str | None,
) -> str | None:
    if serialized:
        # LangChain stuffs the model name in several places depending on provider.
        kwargs = serialized.get("kwargs", {}) or {}
        for key in ("model", "model_name", "deployment_name"):
            if key in kwargs and kwargs[key]:
                return str(kwargs[key])
    if metadata and (m := metadata.get("ls_model_name") or metadata.get("model")):
        return str(m)
    return hint


def _last_human_text(messages: list[list]) -> str:
    """Pull the last user message text out of the nested message lists
    LangChain passes to on_chat_model_start."""
    if not messages:
        return ""
    flat = messages[-1] if isinstance(messages[-1], list) else messages
    for m in reversed(flat):
        if getattr(m, "type", None) == "human":
            c = getattr(m, "content", "")
            return c if isinstance(c, str) else str(c)
    return ""


def _final_ai_message(response: Any) -> Any | None:
    """Extract the final AIMessage from a LangChain ``LLMResult``-like response."""
    gens = getattr(response, "generations", None)
    if not gens:
        return None
    last_gen = gens[-1]
    if not last_gen:
        return None
    msg_or_gen = last_gen[-1]
    return getattr(msg_or_gen, "message", msg_or_gen)
