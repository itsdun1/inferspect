"""Service: agent registry + operator-triggered kill.

Reads connected-agent listings from ClickHouse (the source of truth for
"this agent has actually been observing traffic"). For kills, forwards to
the ingestion service's ``/v1/control/kill`` endpoint using the configured
SDK key — insights-api doesn't itself hold a Valkey connection.
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from insights_api.config import settings
from insights_api.repositories import clickhouse_repo as repo
from insights_api.services import anchor as anchor_svc

log = logging.getLogger(__name__)


async def list_host_fingerprints(
    ch_client: Any,
    *,
    host_id: str,
    window_hours: int = 1,
    limit: int = 20,
    client: str | None = None,
) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(hours=window_hours)
    rows = await repo.recent_fingerprints_for_host(
        ch_client,
        host_id=host_id,
        since=since,
        limit=limit,
        client=client,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        first_seen = r.get("first_seen")
        last_seen = r.get("last_seen")
        # Strip the JSON wrapping bytes from the preview so the UI shows
        # something readable (the captured wire body is raw {"messages":...}).
        preview = (r.get("sample_preview") or "")[:160]
        out.append({
            "fingerprint": r.get("fingerprint"),
            "first_seen": first_seen.isoformat() if hasattr(first_seen, "isoformat") else first_seen,
            "last_seen": last_seen.isoformat() if hasattr(last_seen, "isoformat") else last_seen,
            "request_count": int(r.get("request_count") or 0),
            "preview": preview,
            "model": r.get("model"),
            "provider": r.get("provider"),
            "distinct_pids": int(r.get("distinct_pids") or 0),
        })
    return out


async def list_agents(
    ch_client: Any,
    *,
    client: str | None = None,
    window_hours: int = 1,
) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(hours=window_hours)
    rows = await repo.connected_agents(ch_client, since=since, client=client)
    out: list[dict[str, Any]] = []
    for r in rows:
        last_seen = r.get("last_seen")
        is_live = False
        if isinstance(last_seen, datetime):
            is_live = (datetime.now(UTC) - last_seen.astimezone(UTC)).total_seconds() < 120
        out.append({
            "host_id": r.get("host_id"),
            "last_seen": last_seen.isoformat() if isinstance(last_seen, datetime) else last_seen,
            "is_live": is_live,
            "event_count": int(r.get("event_count") or 0),
            "container_id": r.get("container_id"),
            "distinct_pids": int(r.get("distinct_pids") or 0),
            "distinct_providers": int(r.get("distinct_providers") or 0),
        })
    return out


async def kill_fingerprint(
    ch_client: Any,
    *,
    host_id: str,
    fingerprint: str,
    reason: str,
    operator_id: str,
    client: str,
    ttl_seconds: int = 3600,
) -> dict[str, Any]:
    if not fingerprint or len(fingerprint) != 64:
        raise ValueError("fingerprint must be a 64-char hex SHA256")

    # Try the Phase G.4 anchor flow first: extract the latest user message
    # from the most recent captured wire body for this (host, fingerprint)
    # and send it as the kernel-side content anchor. Falls back to
    # block_fingerprint when no preview is available (e.g., agent restart
    # cleared the row that carried input_preview).
    preview = await repo.preview_for_fingerprint(
        ch_client, host_id=host_id, fingerprint=fingerprint, client=client,
    )
    if preview:
        try:
            anchor_bytes, expected_hash = anchor_svc.build_full_anchor(preview)
        except ValueError as exc:
            log.warning(
                "fingerprint %s anchor extraction failed (%s) — falling back to block_fingerprint",
                fingerprint[:16], exc,
            )
        else:
            url = settings.ingestion_control_url.rstrip("/") + "/kill-anchor"
            headers = {}
            if settings.sdk_api_key:
                headers["X-Sdk-Key"] = settings.sdk_api_key
            payload = {
                "host_id": host_id,
                "anchor_b64": base64.b64encode(anchor_bytes).decode("ascii"),
                "expected_hash_b64": base64.b64encode(expected_hash).decode("ascii"),
                "reason": reason,
                "ttl_seconds": ttl_seconds,
                "operator_id": operator_id,
            }
            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                body = resp.json()
            try:
                await repo.record_enforcement_event(
                    ch_client,
                    host_id=host_id,
                    fingerprint=fingerprint,
                    command="block_anchor",
                    reason=reason,
                    source="operator",
                    client_name=client or "",
                    operator_id=operator_id,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("enforcement_events insert failed (kill still issued): %s", exc)
            return body

    url = settings.ingestion_control_url.rstrip("/") + "/kill"
    headers: dict[str, str] = {}
    if settings.sdk_api_key:
        headers["X-Sdk-Key"] = settings.sdk_api_key
    payload = {
        "host_id": host_id,
        "fingerprint": fingerprint,
        "reason": reason,
        "ttl_seconds": ttl_seconds,
        "operator_id": operator_id,
    }
    async with httpx.AsyncClient(timeout=5.0) as http:
        resp = await http.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        body = resp.json()

    try:
        await repo.record_enforcement_event(
            ch_client,
            host_id=host_id,
            fingerprint=fingerprint,
            command="block_fingerprint",
            reason=reason,
            source="operator",
            client_name=client or "",
            operator_id=operator_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("enforcement_events insert failed (kill still issued): %s", exc)

    return body


async def kill_session(
    ch_client: Any,
    *,
    session_id: str,
    operator_id: str,
    client: str,
    reason: str = "operator_kill",
    ttl_seconds: int = 3600,
) -> dict[str, Any]:
    """Per-conversation kill using Phase G.4 content-anchor enforcement.

    Look up the most recent captured wire body for this session, extract
    the last few messages as the byte anchor, compute the rolling hash for
    Layer-2 verification, and enqueue the kill via /v1/control/kill-anchor.
    Falls back to fingerprint-pattern kill if the body can't be parsed
    (older captures without full input_preview).
    """
    info = await repo.session_fingerprint(ch_client, session_id=session_id, client=client)
    if not info:
        raise ValueError(
            "no ebpf-agent observation for this session — "
            "the agent must be running on the host that served this conversation"
        )
    # clickhouse-connect can return FixedString columns as bytes; coerce
    # defensively so the JSON serializer doesn't choke downstream.
    host_id_raw = info["host_id"]
    host_id = host_id_raw.decode() if isinstance(host_id_raw, (bytes, bytearray)) else str(host_id_raw)
    fingerprint_raw = info.get("fingerprint") or ""
    fingerprint = fingerprint_raw.decode() if isinstance(fingerprint_raw, (bytes, bytearray)) else str(fingerprint_raw)
    input_preview_raw = info.get("input_preview") or ""
    input_preview = input_preview_raw.decode() if isinstance(input_preview_raw, (bytes, bytearray)) else str(input_preview_raw)

    # Try Phase G.4 first — extract content anchor + expected rolling hash.
    try:
        anchor_bytes, expected_hash = anchor_svc.build_full_anchor(input_preview)
    except ValueError as exc:
        log.warning(
            "session %s anchor extraction failed (%s) — falling back to fingerprint kill",
            session_id, exc,
        )
        return await kill_fingerprint(
            ch_client,
            host_id=host_id,
            fingerprint=fingerprint,
            reason=reason,
            operator_id=operator_id,
            client=client,
            ttl_seconds=ttl_seconds,
        )

    url = settings.ingestion_control_url.rstrip("/") + "/kill-anchor"
    headers: dict[str, str] = {}
    if settings.sdk_api_key:
        headers["X-Sdk-Key"] = settings.sdk_api_key
    payload = {
        "host_id": host_id,
        "anchor_b64": base64.b64encode(anchor_bytes).decode("ascii"),
        "expected_hash_b64": base64.b64encode(expected_hash).decode("ascii"),
        "reason": reason,
        "ttl_seconds": ttl_seconds,
        "operator_id": operator_id,
    }
    async with httpx.AsyncClient(timeout=5.0) as http:
        resp = await http.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        body = resp.json()

    # Audit: tag with fingerprint so the existing enforcement-events query
    # still groups this kill into the right operator's view. The kernel-side
    # matching is by anchor, but operationally we still record fingerprint
    # for cross-reference.
    try:
        await repo.record_enforcement_event(
            ch_client,
            host_id=host_id,
            fingerprint=fingerprint,
            command="block_anchor",
            reason=reason,
            source="operator",
            client_name=client or "",
            operator_id=operator_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("enforcement_events insert failed (kill still issued): %s", exc)

    return body
