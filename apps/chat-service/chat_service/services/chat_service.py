"""Chat orchestration via a LangGraph agent.

The agent itself is built per-request (cheap; just wires the model + tools
into a compiled graph). We instrument it with ``SDKCallback`` so every LLM
call inside the agent emits one ``inference`` log and every tool dispatch
emits one ``tool_execution`` log — even when the agent loops (think → tool →
think again).

Streaming uses ``agent.astream_events("v2")``. The cancellation flow is
unchanged: we register the current asyncio task in ``StreamRegistry`` so
``POST /conversations/{id}/cancel`` can ``task.cancel()`` us.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, status
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from sqlalchemy.ext.asyncio import AsyncSession

from chat_service.cancellation import StreamRegistry
from chat_service.config import settings
from chat_service.db.models import Conversation, Message, User
from chat_service.llm.agent import build_agent
from chat_service.llm.langchain_adapter import provider_for, to_lc_messages
from chat_service.llm.memory import load_window
from chat_service.llm.prompts import SYSTEM_PROMPT
from chatbot_sdk.integrations.langchain import SDKCallback
from chat_service.llm.raw_providers import (
    RawProviders,
    is_raw_model,
    provider_for_raw,
)
from chat_service.llm.tools import DEFAULT_TOOLS
from chat_service.repositories import conversation_repository, message_repository
from chat_service.schemas import SendMessageResponse

if TYPE_CHECKING:
    from chatbot_sdk import InferenceLogger


class ChatService:
    def __init__(
        self,
        *,
        logger: "InferenceLogger | None" = None,
        registry: StreamRegistry | None = None,
        raw_providers: RawProviders | None = None,
    ) -> None:
        self._logger = logger
        self._registry = registry or StreamRegistry()
        # Pre-built at boot in main.lifespan. None only in tests or when
        # the SDK logger wasn't available — the raw-* models then 503.
        self._raw = raw_providers or RawProviders(logger=logger)

    @property
    def registry(self) -> StreamRegistry:
        return self._registry

    # ─── Non-streaming ───────────────────────────────────────────
    async def send_message(
        self,
        *,
        session: AsyncSession,
        user: User,
        conversation_id: uuid.UUID | None,
        content: str,
        model: str | None = None,
    ) -> SendMessageResponse:
        convo, chosen_model = await self._prepare(
            session=session, user=user, conversation_id=conversation_id, model=model
        )
        user_msg = await message_repository.create(
            session, conversation_id=convo.id, role="user", content=content
        )
        await conversation_repository.bump_for_message(session, convo.id, model=chosen_model)

        history = await load_window(session, convo.id)

        if is_raw_model(chosen_model):
            # Direct OpenAI / Anthropic via the pre-built RawProviders
            # (instrumented once at boot in main.lifespan). No LangChain,
            # no tools, no agent loop. logger.context(...) inside chat()
            # stamps conversation_id/user_id on the span.
            raw = await self._raw.chat(
                model=chosen_model,
                system_prompt=SYSTEM_PROMPT,
                history=history,
                conversation_id=convo.id,
                user_id=user.id,
            )
            assistant_content = raw.content
            inference_request_id = None
        else:
            lc_messages = [SystemMessage(content=SYSTEM_PROMPT), *to_lc_messages(history)]
            agent = build_agent(chosen_model, tools=DEFAULT_TOOLS)
            callback = self._make_callback(convo.id, user.id, chosen_model)

            result: dict[str, Any] = await agent.ainvoke(
                {"messages": lc_messages},
                config={"callbacks": [callback] if callback else []},
            )

            # The final message in the agent's state is the assistant response we
            # show to the user. Anything earlier may be tool calls and tool results.
            final_ai = _last_ai_message(result.get("messages", []))
            assistant_content = _content_of(final_ai)
            inference_request_id = getattr(callback, "last_inference_request_id", None)

        assistant_msg = await message_repository.create(
            session,
            conversation_id=convo.id,
            role="assistant",
            content=assistant_content,
            inference_request_id=inference_request_id,
        )
        await conversation_repository.bump_for_message(session, convo.id, model=chosen_model)

        return SendMessageResponse(
            conversation_id=convo.id,
            user_message=_msg_dto(user_msg),
            assistant_message=_msg_dto(assistant_msg),
            inference_request_id=inference_request_id,
            latency_ms=None,
            total_tokens=None,
        )

    # ─── Shared prep ─────────────────────────────────────────────
    async def _prepare(
        self,
        *,
        session: AsyncSession,
        user: User,
        conversation_id: uuid.UUID | None,
        model: str | None,
    ) -> tuple[Conversation, str]:
        convo = await self._resolve_conversation(
            session=session, user=user, conversation_id=conversation_id, model=model
        )
        chosen_model = model or convo.model or settings.default_model
        return convo, chosen_model

    async def _resolve_conversation(
        self,
        *,
        session: AsyncSession,
        user: User,
        conversation_id: uuid.UUID | None,
        model: str | None,
    ) -> Conversation:
        if conversation_id is None:
            return await conversation_repository.create(
                session, user_id=user.id, model=model or settings.default_model
            )
        convo = await conversation_repository.get_for_user(session, conversation_id, user.id)
        if convo is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found")
        if convo.status == "cancelled":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="conversation cancelled")
        return convo

    def _make_callback(self, conv_id: uuid.UUID, user_id: uuid.UUID, model: str) -> SDKCallback | None:
        if self._logger is None:
            return None
        return SDKCallback(
            sdk=self._logger,
            conversation_id=conv_id,
            user_id=user_id,
            model_hint=model,
        )


# ─── Streaming ───────────────────────────────────────────────────
async def stream_send(
    chat_svc: ChatService,
    *,
    session: AsyncSession,
    user: User,
    conversation_id: uuid.UUID | None,
    content: str,
    model: str | None,
) -> AsyncIterator[str]:
    """Stream assistant chunks as SSE.

    Events emitted:
      start       — conversation/user_message ids
      delta       — partial assistant text (one or more)
      tool_call   — model decided to invoke a tool (with name + args preview)
      tool_result — tool returned (with result preview)
      done        — final assistant message persisted
      cancelled   — task was cancelled; partial assistant message persisted
    """
    convo, chosen_model = await chat_svc._prepare(
        session=session, user=user, conversation_id=conversation_id, model=model
    )
    user_msg = await message_repository.create(
        session, conversation_id=convo.id, role="user", content=content
    )
    await conversation_repository.bump_for_message(session, convo.id, model=chosen_model)
    await session.commit()

    raw_path = is_raw_model(chosen_model)
    yield _sse("start", {
        "conversation_id": str(convo.id),
        "user_message_id": str(user_msg.id),
        "model": chosen_model,
        "provider": provider_for_raw(chosen_model) if raw_path else provider_for(chosen_model),
    })

    history = await load_window(session, convo.id)

    chunks: list[str] = []
    cancelled = False
    callback = None

    try:
        async with chat_svc._registry.register_current_task(convo.id):
            if raw_path:
                # Direct provider streaming via the pre-built RawProviders
                # factory. instrument() ran once at boot — the client method
                # is already monkey-patched and emits inference logs as it
                # streams. No LangChain agent, no tools, no callback.
                async for text in chat_svc._raw.stream_chat(
                    model=chosen_model,
                    system_prompt=SYSTEM_PROMPT,
                    history=history,
                    conversation_id=convo.id,
                    user_id=user.id,
                ):
                    if text:
                        chunks.append(text)
                        yield _sse("delta", {"content": text})
            else:
                lc_messages = [SystemMessage(content=SYSTEM_PROMPT), *to_lc_messages(history)]
                agent = build_agent(chosen_model, tools=DEFAULT_TOOLS)
                callback = chat_svc._make_callback(convo.id, user.id, chosen_model)

                async for event in agent.astream_events(
                    {"messages": lc_messages},
                    version="v2",
                    config={"callbacks": [callback] if callback else []},
                ):
                    kind = event.get("event")
                    if kind == "on_chat_model_stream":
                        chunk = event.get("data", {}).get("chunk")
                        text = _content_of(chunk)
                        if text:
                            chunks.append(text)
                            yield _sse("delta", {"content": text})
                    elif kind == "on_tool_start":
                        name = event.get("name", "")
                        raw_input = event.get("data", {}).get("input")
                        yield _sse("tool_call", {
                            "name": name,
                            "args": _safe_args(raw_input),
                        })
                    elif kind == "on_tool_end":
                        name = event.get("name", "")
                        out = event.get("data", {}).get("output")
                        yield _sse("tool_result", {
                            "name": name,
                            "result": _tool_result_text(out),
                        })

        full = "".join(chunks)
        request_id = getattr(callback, "last_inference_request_id", None) if callback else None
        assistant_msg = await message_repository.create(
            session,
            conversation_id=convo.id,
            role="assistant",
            content=full,
            inference_request_id=request_id,
            status="complete",
        )
        await conversation_repository.bump_for_message(session, convo.id, model=chosen_model)
        await session.commit()

        yield _sse("done", {
            "assistant_message_id": str(assistant_msg.id),
            "inference_request_id": str(request_id) if request_id else None,
            "tool_calls": getattr(callback, "tool_calls_made", []) if callback else [],
        })

    except asyncio.CancelledError:
        cancelled = True
        await session.rollback()
        partial = "".join(chunks)
        request_id = getattr(callback, "last_inference_request_id", None) if callback else None
        assistant_msg = await message_repository.create(
            session,
            conversation_id=convo.id,
            role="assistant",
            content=partial or "(cancelled before any output)",
            inference_request_id=request_id,
            status="cancelled",
        )
        await conversation_repository.bump_for_message(session, convo.id, model=chosen_model)
        await session.commit()
        yield _sse("cancelled", {
            "assistant_message_id": str(assistant_msg.id),
            "partial_content": partial,
        })


# ─── helpers ─────────────────────────────────────────────────────
def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _content_of(obj: Any) -> str:
    if obj is None:
        return ""
    content = getattr(obj, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            c if isinstance(c, str) else str(c.get("text", ""))
            for c in content
            if isinstance(c, (str, dict))
        )
    if isinstance(obj, str):
        return obj
    return ""


def _last_ai_message(messages: list) -> AIMessage | None:
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            return m
    return None


def _tool_result_text(value: Any) -> str:
    """Extract a clean string from whatever LangGraph hands us on_tool_end.

    Common shapes:
      - plain str
      - ToolMessage with .content (str or list)
      - dict with 'content' key
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return _truncate(value, 500)
    content = getattr(value, "content", None)
    if isinstance(content, str):
        return _truncate(content, 500)
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict) and "text" in c:
                parts.append(str(c["text"]))
        return _truncate("".join(parts), 500)
    if isinstance(value, dict) and "content" in value:
        return _truncate(str(value["content"]), 500)
    # Fallback to str() but strip the noisy ToolMessage(content='...') wrapper if present.
    s = str(value)
    if s.startswith("content='") or s.startswith('content="'):
        # Extract inner string up to first non-escaped closing quote.
        try:
            import ast
            # Strip up to "content=" then eval the literal.
            literal = s[len("content="):]
            # Stop at first newline / structural boundary.
            for cut in ["' additional_kwargs=", '" additional_kwargs=', "' response_metadata=", '" response_metadata=']:
                idx = literal.find(cut)
                if idx > 0:
                    literal = literal[: idx + 1]
                    break
            extracted = ast.literal_eval(literal)
            if isinstance(extracted, str):
                return _truncate(extracted, 500)
        except (ValueError, SyntaxError):
            pass
    return _truncate(s, 500)


def _safe_args(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _truncate(value, 500)
    try:
        return _truncate(json.dumps(value, default=str), 500)
    except (TypeError, ValueError):
        return _truncate(str(value), 500)


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


def _msg_dto(m: Message):
    from chat_service.schemas import MessageDTO

    return MessageDTO.model_validate(m)
