"""Chat endpoints: non-streaming and streaming (SSE)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from chat_service.deps import ChatServiceDep, CurrentUserDep, SessionDep
from chat_service.schemas import SendMessageRequest, SendMessageResponse
from chat_service.services.chat_service import stream_send

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=SendMessageResponse)
async def send_message(
    body: SendMessageRequest,
    session: SessionDep,
    user: CurrentUserDep,
    svc: ChatServiceDep,
) -> SendMessageResponse:
    return await svc.send_message(
        session=session,
        user=user,
        conversation_id=body.conversation_id,
        content=body.content,
        model=body.model,
    )


@router.post("/stream")
async def send_message_stream(
    body: SendMessageRequest,
    session: SessionDep,
    user: CurrentUserDep,
    svc: ChatServiceDep,
):
    """SSE stream: yields ``start``, then ``delta`` per chunk, then ``done`` or
    ``cancelled``. Closing the connection or hitting the cancel endpoint stops
    the stream — the SDK records ``status='cancelled'`` and the partial
    assistant message is persisted."""
    generator = stream_send(
        svc,
        session=session,
        user=user,
        conversation_id=body.conversation_id,
        content=body.content,
        model=body.model,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
