"""Anomaly detection — rolling z-score on latency + error-rate per (provider, model).

Algorithm
---------
1. Pull the time-series from ``mv_inference_5m`` for the requested window plus
   a one-hour lookback. Each row is a 5-min bucket for a (provider, model).
2. For each (provider, model) series, walk buckets in chronological order.
   For every bucket where we have at least ``MIN_BASELINE_BUCKETS`` prior
   buckets within the lookback window (rolling 1h), compute:
        mean = mean(prior values)
        std  = sample std-dev of prior values
        z    = (value - mean) / std    when std > 0
3. Flag any bucket where ``abs(z) > Z_THRESHOLD`` (default 2.0) on the chosen
   metric. We expose anomalies for both latency (p95) and error_rate
   (error_count / req_count) in the same response — clients can filter.

Why we compute in Python rather than SQL: the rolling-window quantile-merge
math in pure SQL is awkward, and the number of buckets per series in a 1h
window is small (12). The cost is negligible and the code is testable.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import timedelta
from typing import Any

from insights_api.repositories import clickhouse_repo as repo
from insights_api.services.window import parse_window, since_for

Z_THRESHOLD = 2.0
MIN_BASELINE_BUCKETS = 3
LOOKBACK = timedelta(hours=1)


async def anomalies(
    ch_client: Any, *, window: str, client: str | None = None
) -> dict[str, Any]:
    # We expand the query window backwards by LOOKBACK so the first few buckets
    # inside the user-requested window still have a baseline.
    window_delta = parse_window(window)
    extended_window = window_delta + LOOKBACK
    extended_window_str = f"{int(extended_window.total_seconds())}s"
    since_extended = since_for(extended_window_str)
    user_since = since_for(window)

    rows = await repo.anomaly_series(ch_client, since=since_extended, client=client)

    # Group rows by (provider, model) and sort by bucket within each group.
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["provider"], row["model"])].append(row)
    for series in grouped.values():
        series.sort(key=lambda r: r["bucket"])

    findings: list[dict[str, Any]] = []
    for (provider, model), series in grouped.items():
        findings.extend(
            _scan_series(provider=provider, model=model, series=series, user_since=user_since)
        )

    findings.sort(key=lambda f: (f["bucket"], f["provider"], f["model"]))
    return {"window": window, "z_threshold": Z_THRESHOLD, "anomalies": findings}


def _scan_series(
    *,
    provider: str,
    model: str,
    series: list[dict[str, Any]],
    user_since,
) -> list[dict[str, Any]]:
    """Yield anomaly records for buckets inside the user-requested window."""
    findings: list[dict[str, Any]] = []

    latency_vals: list[float] = []
    error_rate_vals: list[float] = []

    for row in series:
        bucket = row["bucket"]
        latency = float(row.get("p95_latency") or 0.0)
        req = int(row.get("req_count") or 0)
        errs = int(row.get("error_count") or 0)
        err_rate = (errs / req) if req else 0.0

        # Only flag buckets in the *user-visible* window, but we still walk
        # the lookback prefix to build the baseline.
        in_window = bucket >= user_since

        if in_window and len(latency_vals) >= MIN_BASELINE_BUCKETS:
            z_lat = _zscore(latency, latency_vals)
            if z_lat is not None and abs(z_lat) > Z_THRESHOLD:
                findings.append(
                    _finding(provider, model, bucket, "latency_ms", latency, z_lat)
                )
            z_err = _zscore(err_rate, error_rate_vals)
            if z_err is not None and abs(z_err) > Z_THRESHOLD:
                findings.append(
                    _finding(provider, model, bucket, "error_rate", err_rate, z_err)
                )

        latency_vals.append(latency)
        error_rate_vals.append(err_rate)

    return findings


def _zscore(value: float, prior: list[float]) -> float | None:
    """Sample-stdev z-score of ``value`` against ``prior``. None if undefined.

    When the baseline has zero variance (flat series) we can't compute a real
    z-score, but a meaningful deviation from a flat baseline IS anomalous —
    arguably more so. We return ``Z_THRESHOLD + 1.0`` in that case so callers
    flag it, with the sign indicating direction."""
    if len(prior) < MIN_BASELINE_BUCKETS:
        return None
    mean = sum(prior) / len(prior)
    var = sum((x - mean) ** 2 for x in prior) / (len(prior) - 1)
    std = math.sqrt(var)
    if std == 0:
        if value == mean:
            return 0.0
        # Flat baseline + deviation = anomaly. Direction preserved via sign.
        return (Z_THRESHOLD + 1.0) * (1.0 if value > mean else -1.0)
    return (value - mean) / std


def _finding(provider: str, model: str, bucket, metric: str, value: float, z: float) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "bucket": bucket,
        "metric": metric,
        "value": value,
        "z_score": z,
    }
