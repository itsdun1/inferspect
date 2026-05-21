"""Built-in tools the chat agent can call.

These exist to exercise the tool-execution log path end-to-end. The reviewer
sees: model emits tool_calls → agent dispatches → tool runs → tool_execution
log captured by the SDK callback → ClickHouse row.

Each tool returns a string (LangChain's BaseTool contract); the agent feeds
that back to the model as a ToolMessage.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import tool

_KB_DIR = Path(__file__).resolve().parent.parent / "kb"
_TOKEN_RE = re.compile(r"\w+")
_kb_cache: list[tuple[str, str]] | None = None


def _load_kb() -> list[tuple[str, str]]:
    """Read every ``.md`` file in the KB directory once and cache it."""
    global _kb_cache
    if _kb_cache is None:
        docs: list[tuple[str, str]] = []
        if _KB_DIR.is_dir():
            for path in sorted(_KB_DIR.glob("*.md")):
                docs.append((path.stem, path.read_text(encoding="utf-8")))
        _kb_cache = docs
    return _kb_cache


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


@tool
def get_current_time(timezone: str = "UTC") -> str:
    """Return the current date and time in the given IANA timezone.

    Use this when the user asks what time it is, or asks about the current
    date. The ``timezone`` argument must be an IANA name like ``UTC``,
    ``America/New_York``, ``Asia/Tokyo``, or ``Europe/London``.
    """
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return f"Unknown timezone: {timezone!r}. Try 'UTC' or an IANA name like 'America/New_York'."
    now = datetime.now(tz)
    return now.strftime("%Y-%m-%d %H:%M:%S %Z")


@tool
def search_knowledge_base(query: str, k: int = 3) -> str:
    """Search Ollive's knowledge base for documents relevant to ``query``.

    Use this for any question about Ollive's product, coverage, pricing,
    claims process, or who should buy a policy. Returns the top ``k``
    matching documents formatted as ``[doc_name]\\n<content>`` blocks
    separated by blank lines. Returns ``"No matching documents found."``
    when nothing scores above zero.
    """
    docs = _load_kb()
    tokens = _tokenize(query)
    if not docs or not tokens:
        return "No matching documents found."

    scored: list[tuple[int, str, str]] = []
    for name, content in docs:
        haystack = content.lower()
        score = sum(len(re.findall(rf"\b{re.escape(tok)}\b", haystack)) for tok in tokens)
        if score > 0:
            scored.append((score, name, content))

    if not scored:
        return "No matching documents found."

    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[: max(1, k)]
    return "\n\n".join(f"[{name}]\n{content.strip()}" for _, name, content in top)


@tool
def estimate_quote(
    annual_revenue_usd: int,
    use_case: str,
    coverage_limit_usd: int = 2_000_000,
) -> str:
    """Estimate an indicative annual premium range for an Ollive AI liability policy.

    Use this when the user asks "how much does it cost", "what's the price",
    "can I get a quote", or wants a ballpark figure. Returns a *non-binding*
    range — bound premiums are quoted by underwriters after full application.

    Arguments:
      annual_revenue_usd: customer's annual revenue in USD (best estimate is fine)
      use_case: short description like "AI hiring assistant", "RAG over docs",
        "AI customer support", "code generation", "image generation"
      coverage_limit_usd: requested aggregate limit; defaults to $2M
    """
    # Rough rate table — not real underwriting, illustrative only.
    base_rate = 0.012  # 1.2% of limit at $1M revenue
    revenue_factor = max(1.0, (annual_revenue_usd / 1_000_000) ** 0.5)
    # Risk multiplier by use case.
    uc = use_case.lower()
    if any(k in uc for k in ["hiring", "lending", "credit", "housing", "insurance"]):
        risk = 1.6  # algorithmic-bias-heavy verticals
    elif any(k in uc for k in ["medical", "health", "diagnos"]):
        risk = 2.0  # regulated; usually outside appetite, surface as caveat
    elif any(k in uc for k in ["code", "developer", "copilot"]):
        risk = 0.85
    elif any(k in uc for k in ["customer support", "chatbot", "rag", "search"]):
        risk = 1.0
    elif any(k in uc for k in ["image", "video", "generative"]):
        risk = 1.3  # higher copyright/IP exposure
    else:
        risk = 1.1

    midpoint = coverage_limit_usd * base_rate * revenue_factor * risk
    low = int(midpoint * 0.75)
    high = int(midpoint * 1.35)
    suffix = ""
    if risk >= 2.0:
        suffix = " (NOTE: medical-AI use cases may fall outside Ollive's standard appetite; underwriter review required.)"
    return (
        f"Indicative annual premium for ${coverage_limit_usd:,} aggregate limit, "
        f"~${annual_revenue_usd:,} revenue, use case '{use_case}': "
        f"**${low:,} - ${high:,}/yr**. This is a rough estimate, not a bound quote. "
        f"Final premium depends on full underwriting (controls, evals, incident history)."
        f"{suffix}"
    )


DEFAULT_TOOLS = [get_current_time, search_knowledge_base, estimate_quote]
"""Tools made available to the chat agent by default. Extend here as new
tools land — each must be a ``@tool``-decorated function or BaseTool."""
