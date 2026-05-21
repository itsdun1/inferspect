"""Unit tests for the validation_service layer."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from chatbot_sdk.schema import InferenceLog, LogType
from ingestion_service.services.validation_service import parse


def _good_inference_payload() -> dict:
    return {
        "schema_version": "1.0",
        "log_type": "inference",
        "service": "chat-service",
        "request_id": str(uuid4()),
        "provider": "google",
        "model": "gemini-2.5-pro",
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "latency_ms": 100,
        "status": "ok",
    }


def test_parse_inference_log_ok():
    log = parse(_good_inference_payload())
    assert isinstance(log, InferenceLog)
    assert log.log_type == LogType.INFERENCE


def test_parse_unknown_log_type_raises():
    with pytest.raises(ValueError, match="unknown log_type"):
        parse({"log_type": "garbage"})


def test_parse_missing_required_field_raises():
    payload = _good_inference_payload()
    del payload["provider"]
    with pytest.raises(ValueError, match="inference validation"):
        parse(payload)


def test_parse_rejects_non_dict():
    with pytest.raises(ValueError, match="must be an object"):
        parse("not-an-object")  # type: ignore[arg-type]
