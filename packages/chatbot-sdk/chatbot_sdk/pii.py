"""Pure-regex client-side PII redactor.

Run inside the SDK before logs cross the network so PII never leaves the
customer's process unredacted. Each recognizer is intentionally simple — the
ingestion service still runs Presidio as defense-in-depth, but the goal here
is to catch the common cases (email, phone, card, SSN, IBAN, IPv4/IPv6, AWS
access key) with zero new dependencies.

Public API::

    redact_text(text, recognizers=None) -> str
    redact_dict(d, recognizers=None) -> dict

If ``recognizers`` is None we run :data:`DEFAULT_RECOGNIZERS` (which excludes
IPv6 because the IPv6 regex has too many false positives for general text).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

# ─── Recognizer regexes ──────────────────────────────────────────
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Phone: E.164 (+ followed by 7-15 digits) OR common North American shapes.
_PHONE_E164_RE = re.compile(r"\+\d{7,15}")
_PHONE_NA_RE = re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b")

# Credit card: 13-19 digit groups with optional separators. Luhn-validated below.
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# IBAN: country code (2 letters) + 2 check digits + 4-30 alphanumerics.
# Total length 15-34 per ISO 13616. We over-match in the regex and validate
# length in code.
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{4,30}\b")

_IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"
)

# IPv6: basic colon-separated hex groups (also covers ``::`` shorthand by
# allowing empty groups between colons). False-positive heavy; not in default.
_IPV6_RE = re.compile(
    r"\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}\b"
    r"|\b(?:[0-9A-Fa-f]{1,4}:){1,7}:\b"
    r"|::(?:[0-9A-Fa-f]{1,4}:){0,6}[0-9A-Fa-f]{1,4}\b"
)

_AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")


def _luhn_ok(digits: str) -> bool:
    """Return True if a digits-only string passes the Luhn checksum."""
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        n = ord(ch) - 48
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _redact_card(text: str) -> str:
    def sub(match: re.Match[str]) -> str:
        raw = match.group(0)
        digits = re.sub(r"[ \-]", "", raw)
        if _luhn_ok(digits):
            return "<CREDIT_CARD>"
        return raw

    return _CARD_RE.sub(sub, text)


def _redact_iban(text: str) -> str:
    def sub(match: re.Match[str]) -> str:
        raw = match.group(0)
        if 15 <= len(raw) <= 34:
            return "<IBAN_CODE>"
        return raw

    return _IBAN_RE.sub(sub, text)


# Map recognizer name → callable(str) -> str. Order matters: we run more
# specific patterns (cards, IBAN) before generic ones to avoid clobbering.
_RECOGNIZERS: dict[str, Callable[[str], str]] = {
    "CREDIT_CARD": _redact_card,
    "US_SSN": lambda t: _SSN_RE.sub("<US_SSN>", t),
    "IBAN_CODE": _redact_iban,
    "AWS_ACCESS_KEY": lambda t: _AWS_KEY_RE.sub("<AWS_ACCESS_KEY>", t),
    "EMAIL_ADDRESS": lambda t: _EMAIL_RE.sub("<EMAIL_ADDRESS>", t),
    "PHONE_NUMBER": lambda t: _PHONE_NA_RE.sub(
        "<PHONE_NUMBER>", _PHONE_E164_RE.sub("<PHONE_NUMBER>", t)
    ),
    "IPV4": lambda t: _IPV4_RE.sub("<IPV4>", t),
    "IPV6": lambda t: _IPV6_RE.sub("<IPV6>", t),
}

# IPv6 omitted: regex IPv6 detection over arbitrary text is noisy.
DEFAULT_RECOGNIZERS: list[str] = [
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "CREDIT_CARD",
    "US_SSN",
    "IBAN_CODE",
    "IPV4",
    "AWS_ACCESS_KEY",
]


def redact_text(text: str, recognizers: list[str] | None = None) -> str:
    """Return ``text`` with PII spans replaced by ``<RECOGNIZER_NAME>`` tokens."""
    if not isinstance(text, str) or not text:
        return text
    names = recognizers if recognizers is not None else DEFAULT_RECOGNIZERS
    # Run specific-before-generic so e.g. an email containing digits isn't
    # mistaken for a card. The dict above is ordered; preserve that order
    # but filter to the caller's selection.
    for name in _RECOGNIZERS:
        if name in names:
            text = _RECOGNIZERS[name](text)
    return text


def redact_dict(d: dict, recognizers: list[str] | None = None) -> dict:
    """Recursively walk a dict and call :func:`redact_text` on every string."""
    return _redact_value(d, recognizers)  # type: ignore[return-value]


def _redact_value(value: Any, recognizers: list[str] | None) -> Any:
    if isinstance(value, str):
        return redact_text(value, recognizers)
    if isinstance(value, dict):
        return {k: _redact_value(v, recognizers) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, recognizers) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, recognizers) for item in value)
    return value
