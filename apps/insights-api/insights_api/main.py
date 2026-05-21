"""FastAPI app factory + lifespan wiring for the insights API."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import clickhouse_connect
from fastapi import FastAPI

from insights_api.config import settings
from insights_api.controllers import (
    anomalies_controller,
    health_controller,
    metrics_controller,
    sessions_controller,
    tools_controller,
)
from insights_api.deps import ClickHouseClientHolder

log = logging.getLogger(__name__)


async def _ch_client_factory():
    return await clickhouse_connect.get_async_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=settings.clickhouse_db,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    holder = ClickHouseClientHolder(_ch_client_factory)
    app.state.ch_holder = holder

    log.info("insights-api startup complete (ch=%s:%s/%s)",
             settings.clickhouse_host, settings.clickhouse_port, settings.clickhouse_db)
    try:
        yield
    finally:
        await holder.close()
        log.info("insights-api shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(title="Ollive Insights API", version="0.1.0", lifespan=lifespan)

    # CORS — the Next.js frontend reads from this directly. Allow our dev
    # ports + any prod domain set via env.
    from fastapi.middleware.cors import CORSMiddleware

    origins = [
        "http://localhost:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
    ]
    if settings.allowed_origins:
        origins.extend(o.strip() for o in settings.allowed_origins.split(",") if o.strip())

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_controller.router)
    app.include_router(metrics_controller.router)
    app.include_router(sessions_controller.router)
    app.include_router(anomalies_controller.router)
    app.include_router(tools_controller.router)
    return app


app = create_app()
