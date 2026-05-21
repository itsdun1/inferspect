"""Service: parse and validate a log event into a concrete Pydantic model.

Versioned via the ``schema_version`` field on each event. Today there's only
``1.0`` so the migration map is a no-op, but the indirection lets us accept
older clients without rejecting them.
"""

from __future__ import annotations

from typing import Any

from chatbot_sdk.schema import (
    ApplicationLog,
    InferenceLog,
    LogType,
    ToolExecutionLog,
)
from pydantic import ValidationError

LogModel = InferenceLog | ToolExecutionLog | ApplicationLog


_MODEL_BY_LOG_TYPE: dict[LogType, type] = {
    LogType.INFERENCE: InferenceLog,
    LogType.TOOL_EXECUTION: ToolExecutionLog,
    LogType.APPLICATION: ApplicationLog,
}


def _migrate(event: dict[str, Any]) -> dict[str, Any]:
    """Apply forward-migrations on the raw dict if schema_version is older.

    Currently only schema_version='1.0' is known; older versions are accepted
    as-is. New migrations append cases here without touching call-sites.
    """
    version = event.get("schema_version", "1.0")
    if version == "1.0":
        return event
    # Future migrations: if version == "0.9": event["foo"] = ...
    return event


def parse(raw: dict[str, Any]) -> LogModel:
    """Return a typed log model. Raises ``ValueError`` if the payload is not
    parseable — controller turns that into HTTP 422."""
    if not isinstance(raw, dict):
        raise ValueError("event must be an object")
    log_type_raw = raw.get("log_type")
    try:
        log_type = LogType(log_type_raw)
    except ValueError as exc:
        raise ValueError(f"unknown log_type: {log_type_raw!r}") from exc

    model_cls = _MODEL_BY_LOG_TYPE[log_type]
    try:
        return model_cls.model_validate(_migrate(raw))
    except ValidationError as exc:
        # Surface only the first error to keep the response slim.
        first = exc.errors()[0] if exc.errors() else {"msg": "invalid"}
        loc = ".".join(str(p) for p in first.get("loc", ()))
        msg = first.get("msg", "invalid")
        raise ValueError(f"{log_type} validation: {loc}: {msg}") from exc
