"""FastAPI dependency injection for ingestion service.

We hold a single Valkey connection on ``app.state`` and hand out reference
wrappers (repositories/services) per request. The objects themselves are
stateless beyond the broker connection.
"""

from __future__ import annotations

from fastapi import Header, HTTPException, Request, status

from ingestion_service.config import settings
from ingestion_service.repositories.idempotency_repository import IdempotencyRepository
from ingestion_service.repositories.valkey_publisher import ValkeyPublisher
from ingestion_service.services.ingest_service import IngestService
from ingestion_service.services.pii_service import PIIService


# ─── DI lookups ──────────────────────────────────────────────────
def get_publisher(request: Request) -> ValkeyPublisher:
    return request.app.state.publisher  # type: ignore[no-any-return]


def get_idempotency(request: Request) -> IdempotencyRepository:
    return request.app.state.idempotency  # type: ignore[no-any-return]


def get_pii(request: Request) -> PIIService:
    return request.app.state.pii  # type: ignore[no-any-return]


def get_ingest_service(request: Request) -> IngestService:
    return request.app.state.ingest_service  # type: ignore[no-any-return]


# ─── Auth ────────────────────────────────────────────────────────
async def require_api_key(
    x_sdk_key: str | None = Header(default=None, alias="X-Sdk-Key"),
) -> None:
    expected = settings.sdk_api_key
    if not expected:
        return  # no key configured → open for dev
    if x_sdk_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid sdk key")
