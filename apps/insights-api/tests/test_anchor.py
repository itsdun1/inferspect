"""Unit tests for anchor + rolling-hash extraction.

These tests pin the byte-format of the rolling hash; if the agent's
``rollHash`` in tracker.go changes, these need to update in lockstep.
"""

from __future__ import annotations

import hashlib

from insights_api.services.anchor import (
    build_anchor,
    build_full_anchor,
    normalize_content,
    normalize_role,
    parse_wire_body,
    rolling_hash_over,
)


def test_normalize_role_strips_and_lowers():
    assert normalize_role("USER") == "user"
    assert normalize_role("  System ") == "system"
    assert normalize_role("Assistant\n") == "assistant"


def test_normalize_content_collapses_whitespace():
    assert normalize_content("hello   world") == "hello world"
    assert normalize_content("  hi\n\nthere\t\t") == "hi there"


def test_rolling_hash_empty_chain_is_constant():
    # Empty chain should hash to all-zeros seed (32 zero bytes).
    assert rolling_hash_over([]) == b"\x00" * 32


def test_rolling_hash_is_deterministic_and_unique():
    msgs1 = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "what is 2+2?"},
    ]
    msgs2 = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "what is 3+3?"},  # different
    ]
    h1 = rolling_hash_over(msgs1)
    h1_again = rolling_hash_over(msgs1)
    h2 = rolling_hash_over(msgs2)

    assert h1 == h1_again
    assert h1 != h2
    assert len(h1) == 32


def test_rolling_hash_extends_chain():
    base = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    extended = [*base, {"role": "assistant", "content": "hello!"}]

    h_base = rolling_hash_over(base)
    h_ext = rolling_hash_over(extended)
    # The extended hash should NOT equal the base hash; this confirms the
    # chain is moving forward.
    assert h_base != h_ext


def test_parse_wire_body_openai():
    preview = '{"messages":[{"role":"system","content":"S"},{"role":"user","content":"hi"}]}'
    msgs = parse_wire_body(preview)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["content"] == "hi"


def test_parse_wire_body_anthropic_lifts_system_to_messages():
    preview = '{"system":"You are X","messages":[{"role":"user","content":"hi"}]}'
    msgs = parse_wire_body(preview)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "You are X"
    assert msgs[1]["content"] == "hi"


def test_build_anchor_uses_tail_messages():
    messages = [
        {"role": "system", "content": "long system prompt"},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "tell me about quantum physics"},
    ]
    anchor = build_anchor(messages, tail_turns=3)
    # The tail-3 anchor should NOT contain the system prompt.
    assert b"long system prompt" not in anchor
    # It SHOULD contain the last user message.
    assert b"tell me about quantum physics" in anchor
    # And the assistant's previous reply.
    assert b"first answer" in anchor


def test_build_anchor_truncates_to_max():
    messages = [{"role": "user", "content": "x" * 1000}]
    anchor = build_anchor(messages)
    assert len(anchor) <= 256


def test_build_full_anchor_happy_path():
    preview = '{"messages":[{"role":"system","content":"S"},{"role":"user","content":"hello"}]}'
    anchor, expected = build_full_anchor(preview, tail_turns=3)
    assert b"hello" in anchor
    assert len(expected) == 32


def test_build_full_anchor_rejects_unparsable_body():
    import pytest

    with pytest.raises(ValueError):
        build_full_anchor("not json at all", tail_turns=3)
