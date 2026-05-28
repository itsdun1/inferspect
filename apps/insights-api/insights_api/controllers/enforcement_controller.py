"""Controller: /enforcement-events — audit log of every kill."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query

from insights_api.auth import current_active_operator
from insights_api.db.models import Operator
from insights_api.deps import get_ch_client
from insights_api.repositories import clickhouse_repo as repo

router = APIRouter(prefix="/enforcement-events", tags=["enforcement"])


@router.get("")
async def list_enforcement_events(
    window_hours: int = Query(default=24, ge=1, le=24 * 30),
    host_id: str | None = Query(default=None),
    client: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    ch_client: Any = Depends(get_ch_client),
    operator: Operator = Depends(current_active_operator),  # noqa: ARG001
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(hours=window_hours)
    rows = await repo.enforcement_events(
        ch_client,
        since=since,
        host_id=host_id,
        limit=limit,
        client=client,
    )
    return {"events": rows}
