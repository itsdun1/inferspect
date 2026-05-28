"""Controller: /agents — operator-facing fleet view + kill action."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from insights_api.auth import current_active_operator
from insights_api.db.models import Operator
from insights_api.deps import get_ch_client
from insights_api.services import agents_service

router = APIRouter(prefix="/agents", tags=["agents"])


class KillBody(BaseModel):
    fingerprint: str = Field(min_length=64, max_length=64)
    reason: str = "operator_kill"
    ttl_seconds: int = 3600


class KillSessionBody(BaseModel):
    session_id: str
    reason: str = "operator_kill"
    ttl_seconds: int = 3600


@router.get("")
async def list_agents(
    client: str | None = Query(default=None),
    ch_client: Any = Depends(get_ch_client),
    operator: Operator = Depends(current_active_operator),  # noqa: ARG001
) -> dict[str, Any]:
    rows = await agents_service.list_agents(ch_client, client=client)
    return {"agents": rows}


@router.get(
    "/{host_id}/fingerprints",
    summary="Recent fingerprints (= conversations) observed on this host.",
)
async def list_host_fingerprints(
    host_id: str,
    window_hours: int = Query(default=1, ge=1, le=24 * 30),
    limit: int = Query(default=20, ge=1, le=200),
    client: str | None = Query(default=None),
    ch_client: Any = Depends(get_ch_client),
    operator: Operator = Depends(current_active_operator),  # noqa: ARG001
) -> dict[str, Any]:
    rows = await agents_service.list_host_fingerprints(
        ch_client,
        host_id=host_id,
        window_hours=window_hours,
        limit=limit,
        client=client,
    )
    return {"fingerprints": rows}


@router.post(
    "/{host_id}/kill",
    status_code=status.HTTP_202_ACCEPTED,
)
async def kill_on_host(
    host_id: str,
    body: KillBody = Body(...),
    client: str | None = Query(default=None),
    ch_client: Any = Depends(get_ch_client),
    operator: Operator = Depends(current_active_operator),
) -> dict[str, Any]:
    try:
        result = await agents_service.kill_fingerprint(
            ch_client,
            host_id=host_id,
            fingerprint=body.fingerprint,
            reason=body.reason,
            operator_id=str(operator.id),
            client=client,
            ttl_seconds=body.ttl_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@router.post(
    "/kill-session",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Kill the conversation behind a session_id, resolved via the most recent agent observation.",
)
async def kill_session(
    body: KillSessionBody = Body(...),
    client: str | None = Query(default=None),
    ch_client: Any = Depends(get_ch_client),
    operator: Operator = Depends(current_active_operator),
) -> dict[str, Any]:
    try:
        return await agents_service.kill_session(
            ch_client,
            session_id=body.session_id,
            operator_id=str(operator.id),
            client=client,
            reason=body.reason,
            ttl_seconds=body.ttl_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
