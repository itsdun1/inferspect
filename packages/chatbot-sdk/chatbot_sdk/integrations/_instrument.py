"""Shared utilities for vendor auto-instrumentation.

Each ``chatbot_sdk.integrations.<vendor>`` module exposes an
``instrument(client, *, logger)`` helper that monkey-patches the vendor
client's methods. The mechanics — walking the dotted attribute path,
tagging wrapped methods so repeated calls are no-ops — are shared here so
each vendor module stays small and uniform.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from chatbot_sdk.client import current_context

_SENTINEL = "__chatbot_sdk_instrumented__"


def wrap_method(
    obj: Any,
    attr_path: str,
    wrapper_factory: Callable[[Callable[..., Any]], Callable[..., Any]],
) -> None:
    """Replace ``obj.<attr_path>`` with ``wrapper_factory(original)``.

    ``attr_path`` is dot-separated (e.g. ``"chat.completions.create"``); we
    walk to the leaf parent, swap the final attribute, and tag the new
    callable with ``__chatbot_sdk_instrumented__`` so re-instrumenting the
    same client is a no-op.

    If the leaf is already tagged, this function returns without changes.
    """
    parts = attr_path.split(".")
    parent = obj
    for part in parts[:-1]:
        parent = getattr(parent, part)
    leaf_name = parts[-1]
    original = getattr(parent, leaf_name)
    if getattr(original, _SENTINEL, False):
        return  # already wrapped — idempotent
    wrapped = wrapper_factory(original)
    try:
        setattr(wrapped, _SENTINEL, True)
    except (AttributeError, TypeError):
        # ``functools.partial`` and certain bound methods don't allow attrs;
        # fall back to a thin wrapper that does.
        def _tagged(*a: Any, **kw: Any) -> Any:
            return wrapped(*a, **kw)

        _tagged.__chatbot_sdk_instrumented__ = True  # type: ignore[attr-defined]
        wrapped = _tagged  # type: ignore[assignment]
    setattr(parent, leaf_name, wrapped)


def current_ctx_kwargs() -> dict[str, Any]:
    """Return ``{conversation_id, session_id, user_id}`` filtered to non-None.

    Pass directly as ``**kwargs`` into ``logger.inference(...)`` /
    ``logger.tool_call(...)``."""
    ctx = current_context()
    out: dict[str, Any] = {}
    for key in ("conversation_id", "session_id", "user_id"):
        val = ctx.get(key)
        if val is not None:
            out[key] = val
    return out
