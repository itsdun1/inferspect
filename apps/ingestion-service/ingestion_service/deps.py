"""FastAPI dependency injection for ingestion service.

We hold a single Valkey connection on ``app.state`` and hand out reference
wrappers (repositories/services) per request. The objects themselves are
stateless beyond the broker connection.
"""

from __future__ import annotations

from fastapi import Header, HTTPException, Request, status

from ingestion_service.repositories.idempotency_repository import IdempotencyRepository
from ingestion_service.repositories.valkey_publisher import ValkeyPublisher
from ingestion_service.services.auth_service import ApiKeyResolver
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
    request: Request,
    x_sdk_key: str | None = Header(default=None, alias="X-Sdk-Key"),
) -> str:
    """Resolve the inbound SDK key to its client name.

    Returns the resolved ``client_name``. Raises 401 if the key is unknown.
    If no keys are configured (empty map), allow the request through and
    return ``"unknown"`` so local dev with an empty env still works.
    """
    resolver: ApiKeyResolver = request.app.state.api_key_resolver
    if not resolver.has_any_keys:
        return "unknown"
    client_name = resolver.resolve(x_sdk_key)
    if client_name is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid sdk key")
    return client_name
