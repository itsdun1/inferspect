"""Same batch-service pattern as inference-consumer — orchestrates decode →
ClickHouse insert → DLQ on failure for the application_logs stream."""

from __future__ import annotations

import json
import logging
from typing import Any

from app_log_consumer.repositories.clickhouse_writer import ApplicationLogsWriter
from app_log_consumer.repositories.dlq_publisher import DLQPublisher

logger = logging.getLogger(__name__)


class BatchService:
    def __init__(self, *, writer: ApplicationLogsWriter, dlq: DLQPublisher) -> None:
        self._writer = writer
        self._dlq = dlq

    async def handle_batch(self, raw_messages: list[Any]) -> int:
        events: list[dict[str, Any]] = []
        malformed: list[dict[str, Any]] = []

        for raw in raw_messages:
            decoded = _decode(raw)
            if decoded is None:
                malformed.append({"original": str(raw)[:1000]})
                continue
            events.append(decoded)

        if malformed:
            await self._dlq.publish_failed(malformed, error="malformed-message")

        if not events:
            return 0

        try:
            await self._writer.insert_batch(events)
            logger.info("wrote %d application events", len(events))
            return len(events)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ClickHouse insert failed for application batch (n=%d)", len(events))
            await self._dlq.publish_failed(events, error=f"{type(exc).__name__}: {exc}")
            return 0


def _decode(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        if "payload" in raw and isinstance(raw["payload"], str):
            return _try_json(raw["payload"])
        if "log_type" in raw or "ts" in raw:
            return raw
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        parsed = _try_json(raw)
        if isinstance(parsed, dict) and "payload" in parsed and isinstance(parsed["payload"], str):
            return _try_json(parsed["payload"])
        if isinstance(parsed, dict):
            return parsed
    return None


def _try_json(s: str) -> dict[str, Any] | None:
    try:
        out = json.loads(s)
        return out if isinstance(out, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None
