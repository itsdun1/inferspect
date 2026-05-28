"""Anchor + rolling-hash extraction for Phase G.4 kill commands.

The agent's eBPF program does in-kernel content matching against ``anchor``
byte patterns. The agent's user-space verifies kills by computing the
rolling hash of the disrupted buffer and comparing to ``expected_hash``.
This module builds both from a captured wire body.

The rolling-hash construction MUST match the agent's
``internal/fingerprint/tracker.go:rollHash`` byte-for-byte, otherwise the
agent's Layer 2 verification will always report collateral.
"""

from __future__ import annotations

import hashlib
import json
import struct
from typing import Any

# Must match ANCHOR_MAX in apps/inferspect-agent/bpf/ssl_uprobe.c — the BPF
# map's anchor_entry struct is { __u32 len; __u8 bytes[ANCHOR_MAX]; }.
ANCHOR_MAX_BYTES = 128
DEFAULT_TAIL_TURNS = 3
# Must match FIRST_SEG in ssl_uprobe.c — the BPF scan loop skips entries
# whose len < FIRST_SEG (the inner unrolled compare reads exactly that
# many bytes). build_anchor pads short user messages to this length so
# they're never skipped.
_MIN_ANCHOR_BYTES = 16
DOMAIN_TAG = 0x01  # must match rollHash() in tracker.go


def normalize_role(r: str) -> str:
    """Lowercase + strip all whitespace from role string.

    Mirrors ``normalizeRole`` in ``internal/fingerprint/compute.go``.
    """
    out: list[str] = []
    for c in r:
        if c in " \t\r\n":
            continue
        out.append(c.lower())
    return "".join(out)


def normalize_content(s: str) -> str:
    """Collapse all-whitespace runs to a single space, then trim.

    Mirrors ``normalizeContent`` in
    ``internal/fingerprint/canonicalize.go``.
    """
    out: list[str] = []
    last_space = True
    for c in s:
        if c.isspace():
            if not last_space:
                out.append(" ")
                last_space = True
            continue
        out.append(c)
        last_space = False
    return "".join(out).strip()


def extract_message_text(content: Any) -> str:
    """Pull the text out of an OpenAI/Anthropic message content field.

    Content may be a plain string or a list of ``{type:"text",text:"..."}``
    blocks. We concatenate text blocks; anything else (images, tool calls)
    is rendered as the raw JSON string for hash stability.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        if parts:
            return "".join(parts)
    # Fallback: stable JSON for hashing.
    return json.dumps(content, separators=(",", ":"))


def _roll_one(prev: bytes, role: str, content: str) -> bytes:
    """One step of the rolling hash chain. MUST match Go's rollHash.

    Wire format:
        SHA256(prev || 0x01 || u32_le(len(role)) || role || u32_le(len(content)) || content)
    """
    h = hashlib.sha256()
    h.update(prev)
    h.update(bytes([DOMAIN_TAG]))
    role_b = normalize_role(role).encode("utf-8")
    h.update(struct.pack("<I", len(role_b)))
    h.update(role_b)
    content_b = normalize_content(content).encode("utf-8")
    h.update(struct.pack("<I", len(content_b)))
    h.update(content_b)
    return h.digest()


def rolling_hash_over(messages: list[dict[str, Any]]) -> bytes:
    """Compute the rolling hash chain over a canonical message list."""
    h = b"\x00" * 32
    for m in messages:
        role = m.get("role", "")
        content = extract_message_text(m.get("content", ""))
        h = _roll_one(h, role, content)
    return h


def _parse_messages_lenient(s: str) -> list[dict[str, Any]]:
    """Greedy extract of complete ``{...}`` objects inside a ``"messages":[...]``
    array. Stops at the first incomplete trailing object — useful when the
    agent's ``input_preview`` truncated mid-content.
    """
    # Find the start of the messages array (tolerate optional whitespace).
    for needle in ('"messages":[', '"messages": ['):
        idx = s.find(needle)
        if idx != -1:
            break
    else:
        return []
    start = s.find('[', idx) + 1
    msgs: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    pos = start
    n = len(s)
    while pos < n:
        while pos < n and s[pos] in ' \n\r\t,':
            pos += 1
        if pos >= n or s[pos] == ']':
            break
        try:
            obj, end = decoder.raw_decode(s, pos)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            msgs.append(obj)
        pos = end
    return msgs


def parse_wire_body(input_preview: str) -> list[dict[str, Any]]:
    """Pull the messages array out of a captured agent input_preview.

    The agent stamps ``input_preview`` with up to ~16KB of the literal
    request body. Most bodies fit; very long ones may be truncated. Parse
    leniently — if the full JSON doesn't decode, fall back to greedy
    extraction of the messages array.
    """
    try:
        parsed = json.loads(input_preview)
    except json.JSONDecodeError:
        return _parse_messages_lenient(input_preview)
    if isinstance(parsed, dict):
        msgs = parsed.get("messages", [])
        if parsed.get("system"):
            msgs = [{"role": "system", "content": parsed["system"]}, *msgs]
        return msgs if isinstance(msgs, list) else []
    return []


def build_anchor(messages: list[dict[str, Any]], *, tail_turns: int = DEFAULT_TAIL_TURNS) -> bytes:
    """Extract the byte anchor the kernel will scan for.

    Strategy: anchor on the **first user message** of the conversation.
    That message is always present in every subsequent turn's wire body
    (chat-completions APIs require the full history on each call), and it
    lives near the start of the body — well within the kernel's 512-byte
    scan window. Anchoring on the latest message instead would push the
    anchor past the scan window after a few turns.

    The BPF kernel scan rejects anchors shorter than FIRST_SEG (16 bytes).
    Pad short user messages with their JSON wrapper context (``"content":
    "..."``) which appears verbatim in every wire body, so the anchor is
    always long enough to be scanned.

    Collateral: any conversation whose first user message is byte-identical
    to the killed one is also blocked. Different first user messages →
    different anchor → no collateral. For the daemon-only, no-cooperation
    model this is the intended trade-off (see Phase G plan §4b).

    ``tail_turns`` is accepted for backwards compatibility but ignored.
    """
    _ = tail_turns  # noqa: F841 — kept for API stability
    if not messages:
        return b""

    def _pad(text: str) -> bytes:
        raw = text.encode("utf-8")
        if len(raw) >= _MIN_ANCHOR_BYTES:
            return raw[:ANCHOR_MAX_BYTES]
        # Wrap in the JSON pattern the wire body uses around `content`.
        wrapped = f'"content":"{text}"'.encode("utf-8")
        return wrapped[:ANCHOR_MAX_BYTES]

    for m in messages:
        if (m.get("role") or "").lower() == "user":
            text = extract_message_text(m.get("content", ""))
            if text:
                return _pad(text)
    for m in messages:
        if (m.get("role") or "").lower() != "system":
            text = extract_message_text(m.get("content", ""))
            if text:
                return _pad(text)
    return b""


def build_full_anchor(input_preview: str, *, tail_turns: int = DEFAULT_TAIL_TURNS) -> tuple[bytes, bytes]:
    """Convenience: parse a captured body and return (anchor, expected_hash).

    Returns ``(anchor_bytes, rolling_hash_bytes)``.

    Raises ``ValueError`` if no messages could be parsed (caller should
    fall back to a pattern-based kill or refuse).
    """
    messages = parse_wire_body(input_preview)
    if not messages:
        raise ValueError("could not parse messages from input_preview")
    anchor = build_anchor(messages, tail_turns=tail_turns)
    expected = rolling_hash_over(messages)
    if not anchor:
        raise ValueError("anchor is empty (no extractable text content)")
    return anchor, expected
