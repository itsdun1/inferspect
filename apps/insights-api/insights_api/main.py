"""FastAPI app factory + lifespan wiring for the insights API."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import clickhouse_connect
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from insights_api.auth import bootstrap_operator, operator_router
from insights_api.config import settings
from insights_api.controllers import (
    admin_controller,
    anomalies_controller,
    health_controller,
    metrics_controller,
    sessions_controller,
    tools_controller,
)
from insights_api.db.models import Base
from insights_api.db.session import init_engine
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
    # ── ClickHouse (analytics) ────────────────────────────────────
    holder = ClickHouseClientHolder(_ch_client_factory)
    app.state.ch_holder = holder

    # ── Postgres (operators table; cross-tenant reads via SharedBase) ──
    engine = init_engine(settings.database_url)
    async with engine.begin() as conn:
        # Only create our owned tables (`operators`). The `users`/`conversations`
        # tables live under SharedBase and are owned by chat-service.
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    app.state.engine = engine
    app.state.session_local = SessionLocal
    await bootstrap_operator(SessionLocal)

    # ── SDK logger (for synthetic-log generator) ──────────────────
    sdk_logger = None
    try:
        from chatbot_sdk import InferenceLogger

        sdk_logger = InferenceLogger(
            ingestion_url=settings.ingestion_url,
            service="insights-api",
            api_key=settings.sdk_api_key,
        )
        await sdk_logger.start()
    except Exception as exc:  # noqa: BLE001
        log.warning("SDK init failed; synthetic generator unavailable: %s", exc)
        sdk_logger = None
    app.state.sdk_logger = sdk_logger

    log.info(
        "insights-api startup complete (ch=%s:%s/%s, pg=%s)",
        settings.clickhouse_host,
        settings.clickhouse_port,
        settings.clickhouse_db,
        settings.database_url.split("@")[-1],
    )
    try:
        yield
    finally:
        if sdk_logger is not None:
            await sdk_logger.close()
        await engine.dispose()
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
        "http://localhost:3002",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "http://127.0.0.1:3002",
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
    app.include_router(operator_router, prefix="/auth")
    app.include_router(metrics_controller.router)
    app.include_router(sessions_controller.router)
    app.include_router(anomalies_controller.router)
    app.include_router(tools_controller.router)
    app.include_router(admin_controller.router)
    return app


app = create_app()
