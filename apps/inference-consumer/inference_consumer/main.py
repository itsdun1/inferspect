"""Entry point — wires repositories + services + handlers into a FastStream app."""

from __future__ import annotations

import logging

import clickhouse_connect
from faststream import FastStream
from redis.asyncio import Redis

from inference_consumer.config import settings
from inference_consumer.handlers import build_broker_with_handlers
from inference_consumer.repositories.clickhouse_writer import (
    INFERENCE_COLUMNS,
    TOOL_EXECUTION_COLUMNS,
    ClickHouseWriter,
)
from inference_consumer.repositories.dlq_publisher import DLQPublisher
from inference_consumer.services.batch_service import BatchService

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
    inference_writer = ClickHouseWriter(
        _ch_client_factory, table=settings.inference_table, columns=INFERENCE_COLUMNS
    )
    tool_writer = ClickHouseWriter(
        _ch_client_factory, table=settings.tool_execution_table, columns=TOOL_EXECUTION_COLUMNS
    )

    redis = Redis.from_url(settings.valkey_url, decode_responses=True)
    inference_dlq = DLQPublisher(redis, stream=settings.stream_inference_dlq)
    tool_dlq = DLQPublisher(redis, stream=settings.stream_tool_execution_dlq)

    inference_service = BatchService(writer=inference_writer, dlq=inference_dlq, kind="inference")
    tool_service = BatchService(writer=tool_writer, dlq=tool_dlq, kind="tool_execution")

    broker = build_broker_with_handlers(
        inference_batch_handler=inference_service.handle_batch,
        tool_batch_handler=tool_service.handle_batch,
    )
    return FastStream(broker)


app = build_app()
