"""Controller: /insights/anomalies."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from insights_api.deps import get_ch_client
from insights_api.services import anomaly_service

router = APIRouter(prefix="/insights", tags=["insights"])


@router.get("/anomalies")
async def anomalies(
    window: str = Query(default="1h"),
    client: str | None = Query(default=None),
    ch_client: Any = Depends(get_ch_client),
) -> dict[str, Any]:
    try:
        return await anomaly_service.anomalies(ch_client, window=window, client=client)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
