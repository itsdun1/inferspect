"""Session timeline — interleave inference + tool_execution events by started_at."""

from __future__ import annotations

from typing import Any

from insights_api.repositories import clickhouse_repo as repo


async def session_timeline(client: Any, *, session_id: str) -> dict[str, Any]:
    inference_events = await repo.session_inference_events(client, session_id=session_id)
    tool_events = await repo.session_tool_events(client, session_id=session_id)

    timeline: list[dict[str, Any]] = []
    for ev in inference_events:
        timeline.append({"event_type": "inference", **ev})
    for ev in tool_events:
        timeline.append({"event_type": "tool_execution", **ev})

    timeline.sort(key=lambda e: e["started_at"])

    return {
        "session_id": session_id,
        "inference_count": len(inference_events),
        "tool_count": len(tool_events),
        "timeline": timeline,
    }
