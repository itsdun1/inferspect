"""FastAPI app factory + lifespan wiring for the chat service."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from sqlalchemy.ext.asyncio import async_sessionmaker

from chat_service.auth import auth_router, bootstrap_admin
from chat_service.cancellation import StreamRegistry
from chat_service.config import settings
from chat_service.controllers import (
    chat_controller,
    conversation_controller,
    health_controller,
)
from chat_service.db.models import Base
from chat_service.db.session import get_engine, init_engine
from chat_service.llm.sdk_integrations import SDKIntegrations
from chat_service.services.chat_service import ChatService
from chat_service.services.conversation_service import ConversationService

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = init_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    await bootstrap_admin(SessionLocal)

    # SDK logger — optional; if ingestion isn't reachable the SDK buffers
    # and drops gracefully.
    sdk_logger = None
    try:
        from chatbot_sdk import InferenceLogger

        sdk_logger = InferenceLogger(
            ingestion_url=settings.ingestion_url,
            service=settings.service_name,
            api_key=settings.sdk_api_key,
        )
        await sdk_logger.start()
    except Exception as exc:  # noqa: BLE001
        log.warning("SDK init failed; running without inference logging: %s", exc)
        sdk_logger = None

    # Single hub for chat-service ↔ SDK integration. Constructed ONCE at
    # boot. Holds the raw provider clients (instrumented at construction
    # via SDK's instrument()) and exposes `build_langchain_callback(...)`
    # for the LangChain path. After this line, every chat — raw or
    # LangChain — emits inference logs through the shared sdk_logger.
    sdk_integrations = SDKIntegrations(logger=sdk_logger)

    registry = StreamRegistry()
    app.state.engine = engine
    app.state.stream_registry = registry
    app.state.chat_service = ChatService(
        logger=sdk_logger,
        registry=registry,
        sdk_integrations=sdk_integrations,
    )
    app.state.conversation_service = ConversationService()
    app.state.sdk_logger = sdk_logger
    app.state.sdk_integrations = sdk_integrations

    log.info("chat-service startup complete (db=%s)", settings.database_url.split("@")[-1])
    try:
        yield
    finally:
        if sdk_logger is not None:
            await sdk_logger.close()
        await engine.dispose()
        log.info("chat-service shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(title="Ollive Chat Service", version="0.1.0", lifespan=lifespan)

    # CORS — the Next.js dev server lives at :3000 (or :3001 if 3000 is taken).
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://localhost:3001",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:3001",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_controller.router)
    app.include_router(auth_router)
    app.include_router(conversation_controller.router)
    app.include_router(chat_controller.router)
    return app


app = create_app()
