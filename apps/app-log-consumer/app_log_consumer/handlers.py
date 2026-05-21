"""Broker subscription wiring."""

from __future__ import annotations

from typing import Any

from faststream.redis import RedisBroker, StreamSub

from app_log_consumer.config import settings


def build_broker_with_handlers(*, batch_handler) -> RedisBroker:
    broker = RedisBroker(settings.valkey_url)

    @broker.subscriber(
        stream=StreamSub(
            settings.stream_application,
            group=settings.consumer_group,
            consumer=settings.consumer_name,
            batch=True,
            max_records=settings.batch_size,
            polling_interval=settings.polling_interval_ms,
            no_ack=False,
        )
    )
    async def _on_app_log(msgs: list[dict[str, Any]]) -> None:
        await batch_handler(msgs)

    return broker
