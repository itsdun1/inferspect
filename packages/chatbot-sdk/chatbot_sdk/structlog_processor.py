"""Structlog processor that ships application logs through the SDK transport.

Usage::

    import structlog
    from chatbot_sdk import InferenceLogger, LogShippingProcessor

    logger = InferenceLogger(...)
    await logger.start()

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            LogShippingProcessor(transport=logger.transport, service="chat-service"),
            structlog.processors.JSONRenderer(),
        ]
    )

The processor is non-destructive — it submits a copy of the event to the SDK
transport and returns the original dict so downstream processors (e.g. the
JSON renderer that prints to stdout) still see it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from chatbot_sdk.schema import SCHEMA_VERSION, ApplicationLog, LogType
from chatbot_sdk.transport import BatchedLogTransport


class LogShippingProcessor:
    def __init__(
        self,
        *,
        transport: BatchedLogTransport,
        service: str,
        min_level: str = "INFO",
        pii_redact: bool = True,
        pii_recognizers: list[str] | None = None,
    ) -> None:
        self.transport = transport
        self.service = service
        self._min_level = _level_to_int(min_level)
        self._pii_redact = pii_redact
        self._pii_recognizers = pii_recognizers

    def __call__(self, logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        level = str(event_dict.get("level", method_name)).upper()
        if _level_to_int(level) < self._min_level:
            return event_dict
        try:
            ts_raw = event_dict.get("timestamp")
            ts = _parse_ts(ts_raw)
            message = str(event_dict.get("event", ""))
            attrs = {
                k: v
                for k, v in event_dict.items()
                if k not in {"event", "level", "timestamp", "logger"}
            }
            if self._pii_redact:
                from chatbot_sdk.pii import redact_dict, redact_text

                message = redact_text(message, self._pii_recognizers)
                attrs = redact_dict(attrs, self._pii_recognizers)
            log = ApplicationLog(
                schema_version=SCHEMA_VERSION,
                log_type=LogType.APPLICATION,
                service=self.service,
                ts=ts,
                level=level,
                logger=str(event_dict.get("logger", "")),
                message=message,
                attributes=attrs,
            )
            self.transport.submit(log.model_dump(mode="json"))
        except Exception:  # noqa: BLE001 — never break the logging path
            pass
        return event_dict


_LEVEL_INTS = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "WARN": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


def _level_to_int(level: str) -> int:
    return _LEVEL_INTS.get(level.upper(), 20)


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)
