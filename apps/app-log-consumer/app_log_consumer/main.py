"""Entry point."""

from __future__ import annotations

import logging

import clickhouse_connect
from faststream import FastStream
from redis.asyncio import Redis

from app_log_consumer.config import settings
from app_log_consumer.handlers import build_broker_with_handlers
from app_log_consumer.repositories.clickhouse_writer import ApplicationLogsWriter
from app_log_consumer.repositories.dlq_publisher import DLQPublisher
from app_log_consumer.services.batch_service import BatchService

logging.basicConfig(level=logging.INFO)


async def _ch_client_factory():
    return await clickhouse_connect.get_async_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=settings.clickhouse_db,
    )


def build_app() -> FastStream:
    writer = ApplicationLogsWriter(_ch_client_factory, table=settings.application_table)
    redis = Redis.from_url(settings.valkey_url, decode_responses=True)
    dlq = DLQPublisher(redis, stream=settings.stream_application_dlq)
    svc = BatchService(writer=writer, dlq=dlq)
    broker = build_broker_with_handlers(batch_handler=svc.handle_batch)
    return FastStream(broker)


app = build_app()
