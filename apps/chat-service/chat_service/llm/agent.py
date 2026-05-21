"""LangGraph chat agent — wraps the provider-agnostic chat model with a
tool-calling ReAct loop via ``create_react_agent``.

We deliberately keep the graph stateless (no checkpointer). Persistent state
lives in Postgres (the ``messages`` table) — the agent gets the prior history
passed in on each invocation. This avoids two sources of truth.
"""

from __future__ import annotations

from typing import Any, Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.prebuilt import create_react_agent

from chat_service.llm.langchain_adapter import _model_for, provider_for
from chat_service.llm.tools import DEFAULT_TOOLS


def build_agent(
    model_name: str,
    *,
    tools: Sequence[BaseTool] | None = None,
) -> Any:
    """Return a compiled LangGraph ReAct agent for the given model.

    ``tools`` defaults to ``DEFAULT_TOOLS``. Pass an empty list explicitly for
    a tool-free agent (still goes through the graph, but the model will never
    emit ``tool_calls``)."""
    llm: BaseChatModel = _model_for(model_name)
    tools_seq: Sequence[BaseTool] = DEFAULT_TOOLS if tools is None else tools
    return create_react_agent(llm, tools=list(tools_seq))


__all__ = ["build_agent", "provider_for"]
