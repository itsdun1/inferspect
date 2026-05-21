"""Anomaly service tests — verify the z-score detector flags spikes and not flat series."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from insights_api.services import anomaly_service

from tests.conftest import FakeCHClient


def _series(latencies: list[float], errors: list[int] | None = None) -> list[dict]:
    """Build a synthetic mv_inference_5m series ending at now, one row per 5 min."""
    now = datetime.now(UTC).replace(microsecond=0, second=0)
    # Align to 5-minute boundary.
    now = now.replace(minute=(now.minute // 5) * 5)
    rows = []
    n = len(latencies)
    for i, lat in enumerate(latencies):
        bucket = now - timedelta(minutes=5 * (n - 1 - i))
        rows.append({
            "bucket": bucket,
            "provider": "google",
            "model": "gemini-2.5-pro",
            "p95_latency": lat,
            "req_count": 100,
            "error_count": (errors[i] if errors else 0),
        })
    return rows


async def test_flat_latency_series_yields_no_anomalies():
    # 20 buckets of identical latency — no variance, no spikes.
    rows = _series([200.0] * 20)
    client = FakeCHClient([rows])

    res = await anomaly_service.anomalies(client, window="1h")
    assert res["anomalies"] == []


async def test_latency_spike_is_flagged():
    # 15 buckets at 200ms, then one bucket at 5000ms — way above 2σ.
    rows = _series([200.0] * 15 + [5000.0])
    client = FakeCHClient([rows])

    res = await anomaly_service.anomalies(client, window="1h")

    latency_findings = [f for f in res["anomalies"] if f["metric"] == "latency_ms"]
    assert len(latency_findings) >= 1
    spike = latency_findings[-1]
    assert spike["value"] == 5000.0
    assert spike["z_score"] > anomaly_service.Z_THRESHOLD
    assert spike["provider"] == "google"
    assert spike["model"] == "gemini-2.5-pro"


async def test_error_rate_spike_is_flagged():
    # Flat error rate (0%) for 15 buckets, then 50% errors.
    errors = [0] * 15 + [50]
    rows = _series([200.0] * 16, errors=errors)
    client = FakeCHClient([rows])

    res = await anomaly_service.anomalies(client, window="1h")
    err_findings = [f for f in res["anomalies"] if f["metric"] == "error_rate"]
    assert len(err_findings) >= 1
    assert err_findings[-1]["value"] == pytest.approx(0.5)


async def test_short_series_yields_no_anomalies_due_to_baseline_requirement():
    # Only 2 buckets — below MIN_BASELINE_BUCKETS, so no findings.
    rows = _series([200.0, 5000.0])
    client = FakeCHClient([rows])

    res = await anomaly_service.anomalies(client, window="1h")
    assert res["anomalies"] == []
