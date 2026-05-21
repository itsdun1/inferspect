"""Controller: /insights/{latency,throughput,errors,cost,summary,top-conversations}.

Thin layer — validates query params via FastAPI, calls the service, returns JSON.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from insights_api.deps import get_ch_client
from insights_api.services import metrics_service

router = APIRouter(prefix="/insights", tags=["insights"])


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/latency")
async def latency(
    window: str = Query(default="1h"),
    group: str = Query(default="model"),
    client: Any = Depends(get_ch_client),
) -> dict[str, Any]:
    try:
        return await metrics_service.latency(client, window=window, group=group)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/throughput")
async def throughput(
    window: str = Query(default="1h"),
    group: str = Query(default="provider"),
    client: Any = Depends(get_ch_client),
) -> dict[str, Any]:
    try:
        return await metrics_service.throughput(client, window=window, group=group)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/errors")
async def errors(
    window: str = Query(default="24h"),
    sample_size: int = Query(default=5, ge=1, le=20),
    client: Any = Depends(get_ch_client),
) -> dict[str, Any]:
    try:
        return await metrics_service.errors(client, window=window, sample_size=sample_size)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/cost")
async def cost(
    window: str = Query(default="7d"),
    group: str = Query(default="model"),
    client: Any = Depends(get_ch_client),
) -> dict[str, Any]:
    try:
        return await metrics_service.cost(client, window=window, group=group)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/top-conversations")
async def top_conversations(
    metric: str = Query(default="cost"),
    limit: int = Query(default=20, ge=1, le=200),
    window: str = Query(default="24h"),
    client: Any = Depends(get_ch_client),
) -> dict[str, Any]:
    try:
        return await metrics_service.top_conversations(
            client, metric=metric, limit=limit, window=window
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/summary")
async def summary(
    window: str = Query(default="1h"),
    client: Any = Depends(get_ch_client),
) -> dict[str, Any]:
    try:
        return await metrics_service.summary(client, window=window)
    except ValueError as exc:
        raise _bad_request(exc) from exc
