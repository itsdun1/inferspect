"""Admin-only endpoints. Gated by ``current_admin_user``."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from chat_service.deps import AdminUserDep, SessionDep
from chat_service.schemas import ConversationListItemDTO
from chat_service.services import admin_service

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminUserDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    role: str
    is_active: bool
    is_superuser: bool
    created_at: object  # datetime; loose for OpenAPI


class SetRoleRequest(BaseModel):
    role: str = Field(..., pattern="^(user|admin)$")


class SyntheticRequest(BaseModel):
    count: int = Field(100, ge=1, le=10_000)
    error_rate: float = Field(0.05, ge=0, le=1)
    tool_call_rate: float = Field(0.2, ge=0, le=1)
    spread_seconds: int = Field(600, ge=0, le=86_400)


class SyntheticResponse(BaseModel):
    inference_events: int
    tool_events: int
    error_events: int


@router.get("/conversations", response_model=list[ConversationListItemDTO])
async def list_all_conversations(
    session: SessionDep,
    _admin: AdminUserDep,
    limit: int = 100,
    offset: int = 0,
):
    return await admin_service.list_all_conversations(session, limit=limit, offset=offset)


@router.get("/users", response_model=list[AdminUserDTO])
async def list_all_users(
    session: SessionDep,
    _admin: AdminUserDep,
    limit: int = 100,
    offset: int = 0,
):
    return await admin_service.list_all_users(session, limit=limit, offset=offset)


@router.post("/users/{user_id}/role", response_model=AdminUserDTO)
async def set_user_role(
    user_id: uuid.UUID,
    body: SetRoleRequest,
    session: SessionDep,
    _admin: AdminUserDep,
):
    try:
        user = await admin_service.set_user_role(session, user_id, role=body.role)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return user


@router.post("/synthetic", response_model=SyntheticResponse)
async def generate_synthetic(
    body: SyntheticRequest,
    request: Request,
    _admin: AdminUserDep,
):
    sdk = request.app.state.sdk_logger
    if sdk is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SDK logger unavailable — synthetic generation needs it",
        )
    result = await admin_service.generate_synthetic_logs(
        sdk,
        count=body.count,
        error_rate=body.error_rate,
        tool_call_rate=body.tool_call_rate,
        spread_seconds=body.spread_seconds,
    )
    return SyntheticResponse(**result)
