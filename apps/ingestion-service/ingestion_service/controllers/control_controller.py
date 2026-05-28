"""Controller: control-plane endpoints for the eBPF agent.

Three routes:

- ``GET /v1/control/poll`` — long-poll, blocks up to 60s waiting for the
  next command for the calling host. Returns ``{commands, cursor}``.
- ``POST /v1/control/kill`` — enqueues a ``block_fingerprint`` command for a
  named host. Called by insights-api on behalf of the operator UI.
- ``POST /v1/control/heartbeat`` — agent reports liveness + metadata. The
  agent ALSO heartbeats implicitly via the poll endpoint, but the explicit
  POST is useful for the first connect (before any poll).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from ingestion_service.deps import require_api_key
from ingestion_service.services.control_service import ControlService

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/control", tags=["control"])


# ─── DI ───────────────────────────────────────────────────────────────


def get_control_service(request: Request) -> ControlService:
    return request.app.state.control_service  # type: ignore[no-any-return]


# ─── Request/response models ──────────────────────────────────────────


class KillRequest(BaseModel):
    host_id: str
    fingerprint: str = Field(min_length=64, max_length=64)
    reason: str = "operator_kill"
    ttl_seconds: int = 3600
    operator_id: str | None = None


class KillResponse(BaseModel):
    command_id: str
    cursor: str
    fingerprint: str
    host_id: str


class KillAnchorRequest(BaseModel):
    """Phase G.4 — content-anchor kill payload.

    ``anchor_b64`` is the byte pattern the agent's BPF program will scan
    every outgoing SSL_write for; ``expected_hash_b64`` is the 32-byte
    rolling hash the agent verifies in user-space after a kill fires.
    Both are base64 because they're arbitrary bytes inside JSON.
    """

    host_id: str
    anchor_b64: str
    expected_hash_b64: str
    reason: str = "operator_kill"
    ttl_seconds: int = 3600
    operator_id: str | None = None


class KillAnchorResponse(BaseModel):
    command_id: str
    cursor: str
    host_id: str


class HeartbeatRequest(BaseModel):
    host_id: str
    agent_version: str | None = None
    kernel: str | None = None
    btf: bool | None = None
    libssl_path: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


# ─── Routes ───────────────────────────────────────────────────────────


@router.get("/poll", summary="Agent long-poll for control commands.")
async def poll(
    host_id: str = Query(..., min_length=1, max_length=128),
    cursor: str | None = Query(default=None),
    timeout: int = Query(default=60, ge=1, le=300),
    _client_name: str = Depends(require_api_key),
    svc: ControlService = Depends(get_control_service),
) -> dict[str, Any]:
    return await svc.await_commands(host_id, cursor=cursor, timeout_s=timeout)


@router.post(
    "/kill",
    response_model=KillResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Operator-triggered kill — enqueue a block_fingerprint command.",
)
async def kill(
    body: KillRequest = Body(...),
    client_name: str = Depends(require_api_key),
    svc: ControlService = Depends(get_control_service),
) -> KillResponse:
    try:
        result = await svc.kill_fingerprint(
            host_id=body.host_id,
            fingerprint=body.fingerprint,
            reason=body.reason,
            client=client_name,
            ttl_seconds=body.ttl_seconds,
            operator_id=body.operator_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return KillResponse(**result)


@router.post(
    "/kill-anchor",
    response_model=KillAnchorResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Operator-triggered kill — enqueue a block_anchor command (content match).",
)
async def kill_anchor(
    body: KillAnchorRequest = Body(...),
    client_name: str = Depends(require_api_key),
    svc: ControlService = Depends(get_control_service),
) -> KillAnchorResponse:
    try:
        result = await svc.kill_anchor(
            host_id=body.host_id,
            anchor_b64=body.anchor_b64,
            expected_hash_b64=body.expected_hash_b64,
            reason=body.reason,
            client=client_name,
            ttl_seconds=body.ttl_seconds,
            operator_id=body.operator_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return KillAnchorResponse(**result)


@router.post(
    "/heartbeat",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Agent declares liveness; updates the registry.",
)
async def heartbeat(
    body: HeartbeatRequest = Body(...),
    _client_name: str = Depends(require_api_key),
    svc: ControlService = Depends(get_control_service),
) -> dict[str, str]:
    await svc._queue.touch_heartbeat(  # noqa: SLF001 — controller is allowed to reach in
        body.host_id,
        metadata={
            "agent_version": body.agent_version,
            "kernel": body.kernel,
            "btf": body.btf,
            "libssl_path": body.libssl_path,
            **body.extra,
        },
    )
    return {"status": "ok"}
