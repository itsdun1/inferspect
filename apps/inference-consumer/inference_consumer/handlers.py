"""Handlers — the consumer's "controllers". Subscribe to streams and hand
each batch off to BatchService.

We use FastStream's Redis broker with batch subscribers so we get cheap bulk
inserts into ClickHouse rather than one-at-a-time round-trips.
"""

from __future__ import annotations

import logging
from typing import Any

from faststream.redis import RedisBroker, StreamSub

from inference_consumer.config import settings

logger = logging.getLogger(__name__)


def build_broker_with_handlers(
    *,
    inference_batch_handler,
    tool_batch_handler,
) -> RedisBroker:
    """Wire the broker subscriptions to the provided async batch handlers.

    The handlers are passed in so tests can stub them without booting
    ClickHouse / Valkey.
    """
    broker = RedisBroker(settings.valkey_url)

    @broker.subscriber(
        stream=StreamSub(
            settings.stream_inference,
            group=settings.consumer_group,
            consumer=settings.consumer_name,
            batch=True,
            max_records=settings.batch_size,
            polling_interval=settings.polling_interval_ms,
            no_ack=False,
        )
    )
    async def _on_inference(msgs: list[dict[str, Any]]) -> None:
        await inference_batch_handler(msgs)

    @broker.subscriber(
        stream=StreamSub(
            settings.stream_tool_execution,
            group=settings.consumer_group,
            consumer=settings.consumer_name,
            batch=True,
            max_records=settings.batch_size,
            polling_interval=settings.polling_interval_ms,
            no_ack=False,
        )
    )
    async def _on_tool(msgs: list[dict[str, Any]]) -> None:
        await tool_batch_handler(msgs)

    return broker
