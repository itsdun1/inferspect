"""FastAPI app factory + lifespan wiring."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from redis.asyncio import Redis

from ingestion_service.config import settings
from ingestion_service.controllers import control_controller, health_controller, ingest_controller
from ingestion_service.repositories.control_queue import ControlQueue
from ingestion_service.repositories.idempotency_repository import IdempotencyRepository
from ingestion_service.repositories.valkey_publisher import ValkeyPublisher
from ingestion_service.services.auth_service import ApiKeyResolver
from ingestion_service.services.control_service import ControlService
from ingestion_service.services.ingest_service import IngestService
from ingestion_service.services.pii_service import PIIService

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    client = Redis.from_url(settings.valkey_url, decode_responses=True)
    publisher = ValkeyPublisher(
        client,
        stream_inference=settings.stream_inference,
        stream_tool_execution=settings.stream_tool_execution,
        stream_application=settings.stream_application,
        stream_maxlen=settings.stream_maxlen,
    )
    idempotency = IdempotencyRepository(client, ttl_s=settings.idempotency_ttl_s)
    pii = PIIService(
        enabled=settings.pii_enabled,
        entities=[e.strip() for e in settings.pii_recognizers.split(",") if e.strip()],
        template=settings.pii_anonymize_template,
    )
    ingest_service = IngestService(publisher=publisher, idempotency=idempotency, pii=pii)
    api_key_resolver = ApiKeyResolver(settings.sdk_api_keys_json)
    control_queue = ControlQueue(client)
    control_service = ControlService(queue=control_queue)

    app.state.client = client
    app.state.publisher = publisher
    app.state.idempotency = idempotency
    app.state.pii = pii
    app.state.ingest_service = ingest_service
    app.state.api_key_resolver = api_key_resolver
    app.state.control_queue = control_queue
    app.state.control_service = control_service

    log.info("ingestion-service startup complete")
    try:
        yield
    finally:
        await publisher.close()
        log.info("ingestion-service shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Ollive Ingestion Service",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(health_controller.router)
    app.include_router(ingest_controller.router)
    app.include_router(control_controller.router)
    return app


app = create_app()
