"""Unit tests for the IngestService orchestration layer.

We use fakes for the publisher / idempotency repository so the test runs
without a Valkey instance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from chatbot_sdk.schema import LogType
from ingestion_service.services.ingest_service import IngestService, PublishError
from ingestion_service.services.pii_service import PIIService


class _FakePublisher:
    def __init__(self, fail: bool = False) -> None:
        self.published: list[tuple[LogType, dict]] = []
        self._fail = fail

    async def publish(self, log_type, event):
        if self._fail:
            raise RuntimeError("broker down")
        self.published.append((log_type, event))
        return "0-1"

    async def ping(self):  # pragma: no cover — not used here
        return True


class _FakeIdempotency:
    def __init__(self) -> None:
        self.seen: set[str] = set()

    async def mark_or_check(self, request_id: str) -> bool:
        if request_id in self.seen:
            return False
        self.seen.add(request_id)
        return True


def _inference_payload(**overrides) -> dict:
    base = {
        "schema_version": "1.0",
        "log_type": "inference",
        "service": "chat-service",
        "request_id": str(uuid4()),
        "provider": "google",
        "model": "gemini-2.5-pro",
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "latency_ms": 123,
        "status": "ok",
    }
    base.update(overrides)
    return base


@pytest.fixture
def pii_disabled() -> PIIService:
    return PIIService(enabled=False, entities=[])


async def test_accepts_and_publishes_valid_event(pii_disabled):
    pub = _FakePublisher()
    idem = _FakeIdempotency()
    svc = IngestService(publisher=pub, idempotency=idem, pii=pii_disabled)

    payload = _inference_payload()
    res = await svc.ingest_batch(
        service="chat-service",
        sdk_version="0.1.0",
        events=[payload],
        received_at=datetime.now(UTC).isoformat(),
    )

    assert res.accepted == 1
    assert res.duplicates == 0
    assert res.rejected == 0
    assert len(pub.published) == 1
    log_type, event = pub.published[0]
    assert log_type == LogType.INFERENCE
    assert event["request_id"] == payload["request_id"]
    assert "received_at" in event


async def test_duplicate_is_skipped(pii_disabled):
    pub = _FakePublisher()
    idem = _FakeIdempotency()
    svc = IngestService(publisher=pub, idempotency=idem, pii=pii_disabled)

    payload = _inference_payload()

    res1 = await svc.ingest_batch(
        service="chat-service", sdk_version="0.1.0", events=[payload],
        received_at=datetime.now(UTC).isoformat(),
    )
    res2 = await svc.ingest_batch(
        service="chat-service", sdk_version="0.1.0", events=[payload],
        received_at=datetime.now(UTC).isoformat(),
    )

    assert res1.accepted == 1
    assert res2.accepted == 0
    assert res2.duplicates == 1
    assert len(pub.published) == 1


async def test_rejects_unknown_log_type(pii_disabled):
    svc = IngestService(
        publisher=_FakePublisher(),
        idempotency=_FakeIdempotency(),
        pii=pii_disabled,
    )
    bad = {"log_type": "garbage"}
    res = await svc.ingest_batch(
        service="x", sdk_version="0", events=[bad],
        received_at=datetime.now(UTC).isoformat(),
    )
    assert res.rejected == 1
    assert res.events[0].status == "rejected"


async def test_publish_failure_raises_publish_error(pii_disabled):
    svc = IngestService(
        publisher=_FakePublisher(fail=True),
        idempotency=_FakeIdempotency(),
        pii=pii_disabled,
    )
    with pytest.raises(PublishError):
        await svc.ingest_batch(
            service="x", sdk_version="0",
            events=[_inference_payload()],
            received_at=datetime.now(UTC).isoformat(),
        )


async def test_mixed_batch_partial_success(pii_disabled):
    pub = _FakePublisher()
    idem = _FakeIdempotency()
    svc = IngestService(publisher=pub, idempotency=idem, pii=pii_disabled)

    good = _inference_payload()
    bad = _inference_payload()
    del bad["provider"]  # invalid

    res = await svc.ingest_batch(
        service="x", sdk_version="0", events=[good, bad],
        received_at=datetime.now(UTC).isoformat(),
    )

    assert res.accepted == 1
    assert res.rejected == 1
    assert len(pub.published) == 1
