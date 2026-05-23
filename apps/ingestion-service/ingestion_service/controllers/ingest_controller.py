"""Controller: POST /v1/logs.

Thin layer — pulls the IngestService from the DI container, hands over the
request, translates PublishError to 503 with Retry-After.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from ingestion_service.deps import get_ingest_service, require_api_key
from ingestion_service.schemas import IngestResponse, LogEnvelope
from ingestion_service.services.ingest_service import IngestService, PublishError

router = APIRouter(prefix="/v1", tags=["ingest"])


@router.post(
    "/logs",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receive a batch of log events from an SDK client.",
)
async def ingest(
    envelope: LogEnvelope,
    response: Response,
    request: Request,
    client_name: str = Depends(require_api_key),
    ingest_service: IngestService = Depends(get_ingest_service),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
) -> IngestResponse:
    if not envelope.events:
        raise HTTPException(status_code=400, detail="empty events array")

    # We accept up to settings.max_events_per_batch — Pydantic doesn't enforce
    # so we do here for a friendlier error than the broker complaining later.
    if len(envelope.events) > 500:
        raise HTTPException(status_code=413, detail="batch too large (max 500 events)")

    received_at = datetime.now(UTC).isoformat()

    try:
        result = await ingest_service.ingest_batch(
            client=client_name,
            service=envelope.service,
            sdk_version=envelope.sdk_version,
            events=[e.model_dump(mode="json") for e in envelope.events],
            received_at=received_at,
        )
    except PublishError as exc:
        response.headers["Retry-After"] = "2"
        raise HTTPException(status_code=503, detail=f"broker unavailable: {exc}") from exc

    if x_request_id:
        response.headers["X-Request-Id"] = x_request_id
    return result
