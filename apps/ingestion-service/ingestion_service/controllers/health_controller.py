"""Liveness + readiness."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from ingestion_service.deps import get_publisher
from ingestion_service.repositories.valkey_publisher import ValkeyPublisher

router = APIRouter(tags=["health"])


@router.get("/healthz", include_in_schema=False)
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readiness(
    response: Response,
    publisher: ValkeyPublisher = Depends(get_publisher),
) -> dict[str, str]:
    if await publisher.ping():
        return {"status": "ready"}
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "broker-unavailable"}
