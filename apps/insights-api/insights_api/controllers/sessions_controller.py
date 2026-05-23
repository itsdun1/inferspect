"""Controller: /insights/sessions/{session_id}."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from insights_api.deps import get_ch_client
from insights_api.services import session_service

router = APIRouter(prefix="/insights/sessions", tags=["insights"])


@router.get("/{session_id}")
async def session_timeline(
    session_id: str,
    client: str | None = Query(default=None),
    ch_client: Any = Depends(get_ch_client),
) -> dict[str, Any]:
    # Validate UUID — ClickHouse will refuse a malformed UUID anyway, but a
    # 400 here gives a clearer error than a 500 from the driver.
    try:
        uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid session_id") from exc

    return await session_service.session_timeline(
        ch_client, session_id=session_id, client=client
    )
