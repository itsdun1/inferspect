"""Service: orchestrates the ingestion pipeline for a batch of log events.

Pipeline per event:
  1. validate (parse Pydantic model per log_type + apply schema migrations)
  2. redact (PII)
  3. dedup (idempotency: SET NX in Valkey)
  4. publish (XADD to Valkey Stream for the log_type)
  5. record per-event status to return to the SDK

The service layer holds no broker/DB state itself; everything is passed in as
arguments. This is the "transaction-passing" pattern: the controller owns the
unit of work (the request) and hands the collaborators down."""

from __future__ import annotations

import logging
from typing import Any

from ingestion_service.repositories.idempotency_repository import IdempotencyRepository
from ingestion_service.repositories.valkey_publisher import ValkeyPublisher
from ingestion_service.schemas import EventStatus, IngestResponse
from ingestion_service.services.pii_service import PIIService
from ingestion_service.services.validation_service import parse

logger = logging.getLogger(__name__)


class IngestService:
    """Stateless orchestrator. Holds references to its collaborators."""

    def __init__(
        self,
        *,
        publisher: ValkeyPublisher,
        idempotency: IdempotencyRepository,
        pii: PIIService,
    ) -> None:
        self._publisher = publisher
        self._idempotency = idempotency
        self._pii = pii

    async def ingest_batch(
        self,
        *,
        client: str,
        service: str,
        sdk_version: str,
        events: list[dict[str, Any]],
        received_at: str,
    ) -> IngestResponse:
        accepted = 0
        duplicates = 0
        rejected = 0
        statuses: list[EventStatus] = []

        for raw in events:
            try:
                log = parse(raw)
            except ValueError as exc:
                rejected += 1
                statuses.append(
                    EventStatus(
                        request_id=str(raw.get("request_id")) if isinstance(raw, dict) else None,
                        status="rejected",
                        reason=str(exc),
                    )
                )
                continue

            request_id = str(log.request_id)
            event_dict = log.model_dump(mode="json")

            # Enrichment (server-authoritative fields)
            event_dict.setdefault("metadata", {})
            event_dict["received_at"] = received_at
            event_dict["ingest_service"] = service
            # Stamp the resolved tenant on every event before PII runs. The
            # PII redactor only walks known text fields, so this is safe.
            event_dict["client"] = client

            # PII redaction (after validation, before publish — so we never
            # ship raw PII downstream)
            event_dict = self._pii.redact_event(event_dict)

            # Idempotency
            fresh = await self._idempotency.mark_or_check(request_id)
            if not fresh:
                duplicates += 1
                statuses.append(EventStatus(request_id=request_id, status="duplicate"))
                continue

            try:
                await self._publisher.publish(log.log_type, event_dict)
                accepted += 1
                statuses.append(EventStatus(request_id=request_id, status="accepted"))
            except Exception as exc:  # noqa: BLE001
                # Surfacing to the controller; it will translate to 503.
                logger.exception("publish failed for %s", request_id)
                raise PublishError(str(exc)) from exc

        return IngestResponse(
            accepted=accepted,
            duplicates=duplicates,
            rejected=rejected,
            events=statuses,
        )


class PublishError(RuntimeError):
    """Raised when the broker is unreachable — controller turns into 503."""
