"""Controller: /insights/tools."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from insights_api.deps import get_ch_client
from insights_api.services import tools_service

router = APIRouter(prefix="/insights", tags=["insights"])


@router.get("/tools")
async def tools(
    window: str = Query(default="1h"),
    client: Any = Depends(get_ch_client),
) -> dict[str, Any]:
    try:
        return await tools_service.tools(client, window=window)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
