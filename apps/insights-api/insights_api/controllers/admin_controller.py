"""Operator-only admin endpoints. Gated by ``current_active_operator``.

Moved from chat-service in Phase D: these are operator-side queries
(cross-tenant reads + synthetic generation), not customer-product features.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from insights_api.auth.deps import current_active_operator
from insights_api.db.models import Operator
from insights_api.db.session import get_session
from insights_api.services import admin_service

router = APIRouter(prefix="/admin", tags=["admin"])


SessionDep = Annotated[AsyncSession, Depends(get_session)]
OperatorDep = Annotated[Operator, Depends(current_active_operator)]


class ConversationListItemDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    status: str
    model: str | None
    message_count: int
    created_at: datetime
    updated_at: datetime


class AdminUserDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
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
    _operator: OperatorDep,
    limit: int = 100,
    offset: int = 0,
):
    return await admin_service.list_all_conversations(session, limit=limit, offset=offset)


@router.get("/users", response_model=list[AdminUserDTO])
async def list_all_users(
    session: SessionDep,
    _operator: OperatorDep,
    limit: int = 100,
    offset: int = 0,
):
    return await admin_service.list_all_users(session, limit=limit, offset=offset)


@router.post("/users/{user_id}/role", response_model=AdminUserDTO)
async def set_user_role(
    user_id: uuid.UUID,
    body: SetRoleRequest,
    session: SessionDep,
    _operator: OperatorDep,
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
    _operator: OperatorDep,
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
