"""Tests for the chat agent's built-in tools — particularly the small TF RAG
backed by the in-repo knowledge base."""

from __future__ import annotations

import chat_service.llm.tools as tools_module
from chat_service.llm.tools import DEFAULT_TOOLS, search_knowledge_base


def _reset_cache() -> None:
    tools_module._kb_cache = None


def test_search_knowledge_base_returns_relevant_docs():
    _reset_cache()
    result = search_knowledge_base.invoke({"query": "What does Ollive cover?", "k": 2})

    assert "[kb-" in result
    assert "Ollive" in result
    # Two doc blocks separated by a blank line.
    assert result.count("\n\n[") == 1


def test_search_knowledge_base_no_match():
    _reset_cache()
    result = search_knowledge_base.invoke({"query": "zzzzz xyzzy nonsense", "k": 3})
    assert result == "No matching documents found."


def test_search_knowledge_base_top_k_truncates():
    _reset_cache()
    result = search_knowledge_base.invoke({"query": "ollive ai coverage policy claim", "k": 1})
    assert result.count("\n[kb-") == 0  # exactly one block, no second header


def test_default_tools_includes_search():
    names = {t.name for t in DEFAULT_TOOLS}
    assert "search_knowledge_base" in names
    assert "get_current_time" in names
