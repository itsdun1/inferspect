"""LangChain chat-model adapter — wraps one provider per call.

Phase 5 is non-streaming. Phase 8 switches to ``astream`` while keeping the
same adapter shape. We pick the model based on the conversation's ``model``
field (e.g. ``gemini-2.5-pro``, ``gpt-4.1``, ``claude-sonnet-4-6``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from chat_service.config import settings


@dataclass
class LLMResponse:
    """Carrier for what the service / SDK need from a non-streaming call."""

    content: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    tool_calls: list[dict[str, Any]]
    raw: AIMessage


def _model_for(model_name: str) -> BaseChatModel:
    """Return a LangChain chat model for the given short name.

    Provider routing by prefix; missing keys raise eagerly rather than at the
    first call site so the failure surfaces in service init."""
    lower = model_name.lower()
    if lower.startswith("gemini") or lower.startswith("google"):
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=settings.gemini_api_key,
            convert_system_message_to_human=True,
        )
    if lower.startswith("gpt") or lower.startswith("o1") or lower.startswith("openai"):
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model_name, openai_api_key=settings.openai_api_key)
    if lower.startswith("claude") or lower.startswith("anthropic"):
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model_name, anthropic_api_key=settings.anthropic_api_key)
    raise ValueError(f"unrecognized model: {model_name!r}")


def provider_for(model_name: str) -> str:
    lower = model_name.lower()
    if lower.startswith("gemini") or lower.startswith("google"):
        return "google"
    if lower.startswith("gpt") or lower.startswith("o1") or lower.startswith("openai"):
        return "openai"
    if lower.startswith("claude") or lower.startswith("anthropic"):
        return "anthropic"
    return "unknown"


def to_lc_messages(history: list[tuple[str, str]]) -> list[BaseMessage]:
    """Convert ``(role, content)`` pairs to LangChain message objects."""
    out: list[BaseMessage] = []
    for role, content in history:
        if role == "user":
            out.append(HumanMessage(content=content))
        elif role == "assistant":
            out.append(AIMessage(content=content))
        elif role == "system":
            out.append(SystemMessage(content=content))
    return out


async def chat_once(model_name: str, messages: list[BaseMessage]) -> LLMResponse:
    """Single non-streaming call. Returns ``LLMResponse`` with extracted metadata."""
    llm = _model_for(model_name)
    response = await llm.ainvoke(messages)
    assert isinstance(response, AIMessage)

    usage = response.usage_metadata or {}
    finish = (response.response_metadata or {}).get("finish_reason", "") or (
        "tool_calls" if response.tool_calls else "stop"
    )

    content = response.content if isinstance(response.content, str) else str(response.content)

    return LLMResponse(
        content=content,
        provider=provider_for(model_name),
        model=model_name,
        prompt_tokens=int(usage.get("input_tokens", 0) or 0),
        completion_tokens=int(usage.get("output_tokens", 0) or 0),
        finish_reason=str(finish),
        tool_calls=list(response.tool_calls or []),
        raw=response,
    )


async def chat_stream(model_name: str, messages: list[BaseMessage]):
    """Async-iterate over an ``AIMessageChunk`` stream. The caller is expected
    to ``observe_chunk(chunk)`` into an SDK span and forward content to SSE.
    """
    llm = _model_for(model_name)
    async for chunk in llm.astream(messages):
        yield chunk
