"""Service: orchestrate a batch from stream → ClickHouse, with DLQ on failure.

Pure orchestration; the writer and the DLQ publisher are passed in. This keeps
the service unit-testable without a real ClickHouse/Valkey.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from inference_consumer.repositories.clickhouse_writer import ClickHouseWriter
from inference_consumer.repositories.dlq_publisher import DLQPublisher

logger = logging.getLogger(__name__)


class BatchService:
    def __init__(
        self,
        *,
        writer: ClickHouseWriter,
        dlq: DLQPublisher,
        kind: str,  # 'inference' or 'tool_execution' — for logs only
    ) -> None:
        self._writer = writer
        self._dlq = dlq
        self._kind = kind

    async def handle_batch(self, raw_messages: list[dict[str, Any] | str]) -> int:
        """Decode messages, insert into ClickHouse, route insert failures to DLQ.

        Returns the number of events successfully written.
        """
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
            logger.info("wrote %d %s events", len(events), self._kind)
            return len(events)
        except Exception as exc:  # noqa: BLE001 — anything from the driver
            logger.exception("ClickHouse insert failed for %s batch (n=%d)", self._kind, len(events))
            await self._dlq.publish_failed(events, error=f"{type(exc).__name__}: {exc}")
            return 0


def _decode(raw: Any) -> dict[str, Any] | None:
    """Decode a message coming off a Valkey Stream.

    The publisher wraps the event as ``{"payload": "<json>"}`` (a single field
    per stream entry). FastStream passes us either the parsed wrapper dict or
    the raw string depending on the subscriber config; handle both.
    """
    if isinstance(raw, dict):
        if "payload" in raw and isinstance(raw["payload"], str):
            return _try_json(raw["payload"])
        # Already an event dict.
        if "log_type" in raw:
            return raw
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        # Either a JSON wrapper or a bare event.
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
