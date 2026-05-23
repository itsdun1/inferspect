"""Admin orchestration — moved verbatim from chat-service.

Two responsibilities at this layer:
  - cross-tenant reads (every user's conversations, every user)
  - synthetic-log generation: build plausible inference/tool-execution events
    and ship them through the SDK so dashboards/insights have data without
    needing to drive real LLM traffic.

For synthetic generation we bypass the SDK's span context managers and build
log envelopes directly — the spans want real start/end timestamps and would
report bogus latency if we forced backdated values. The transport accepts
any dict matching the wire schema, so we hand-craft those dicts.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from chatbot_sdk.schema import SCHEMA_VERSION, LogType
from insights_api.db.models import Conversation, User

if TYPE_CHECKING:
    from chatbot_sdk import InferenceLogger


# ─── Cross-tenant reads ──────────────────────────────────────────
async def list_all_conversations(
    session: AsyncSession, *, limit: int = 100, offset: int = 0
) -> list[Conversation]:
    res = await session.execute(
        select(Conversation)
        .order_by(desc(Conversation.updated_at))
        .limit(limit)
        .offset(offset)
    )
    return list(res.scalars().all())


async def list_all_users(
    session: AsyncSession, *, limit: int = 100, offset: int = 0
) -> list[User]:
    res = await session.execute(
        select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
    )
    return list(res.scalars().all())


async def set_user_role(session: AsyncSession, user_id: uuid.UUID, *, role: str) -> User:
    if role not in ("user", "admin"):
        raise ValueError(f"invalid role: {role!r}")
    user = await session.get(User, user_id)
    if user is None:
        raise LookupError("user not found")
    user.role = role
    user.is_superuser = role == "admin"
    await session.flush()
    return user


# ─── Synthetic log generation ────────────────────────────────────
_MODELS = [
    ("google", "gemini-2.5-pro"),
    ("google", "gemini-2.5-flash"),
    ("openai", "gpt-4.1"),
    ("openai", "gpt-4o-mini"),
    ("anthropic", "claude-sonnet-4-6"),
]

_TOOLS = ["get_current_time", "search_web", "lookup_user"]
_ERROR_CODES = ["RateLimitError", "TimeoutError", "ServerError"]


async def generate_synthetic_logs(
    sdk: "InferenceLogger",
    *,
    count: int = 100,
    error_rate: float = 0.05,
    tool_call_rate: float = 0.2,
    spread_seconds: int = 600,
) -> dict[str, int]:
    """Submit ``count`` synthetic inference events (+ tool_executions) through
    the SDK transport. Returns a small summary so the admin endpoint can show
    what was generated."""
    inference_count = 0
    tool_count = 0
    error_count = 0

    now = datetime.now(UTC)
    convs = [uuid.uuid4() for _ in range(max(1, count // 10))]
    sess_id = uuid.uuid4()

    for i in range(count):
        provider, model = random.choice(_MODELS)
        conv_id = random.choice(convs)
        request_id = uuid.uuid4()
        started = now - timedelta(seconds=random.randint(0, spread_seconds))
        latency_ms = random.randint(200, 4000)
        finished = started + timedelta(milliseconds=latency_ms)
        ttft_ms = random.randint(50, min(800, latency_ms - 10))
        is_error = random.random() < error_rate
        emits_tool = (not is_error) and random.random() < tool_call_rate
        prompt_tokens = random.randint(20, 400)
        completion_tokens = 0 if is_error else random.randint(20, 500)
        cost_usd = round((prompt_tokens + completion_tokens) * 0.000005, 6)

        if is_error:
            error_count += 1
            status = "error"
            error_code = random.choice(_ERROR_CODES)
            error_message = f"synthetic {error_code}"
            output_preview = ""
            finish_reason = "error"
        else:
            status = "ok"
            error_code = None
            error_message = None
            output_preview = f"synthetic response {i} from {model}"
            finish_reason = "tool_calls" if emits_tool else "stop"

        inference_event: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "log_type": LogType.INFERENCE.value,
            "service": "synthetic",
            "sdk_version": "0.1.0",
            "request_id": str(request_id),
            "conversation_id": str(conv_id),
            "session_id": str(sess_id),
            "user_id": None,
            "provider": provider,
            "model": model,
            "stream": True,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "latency_ms": latency_ms,
            "ttft_ms": ttft_ms,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cost_usd": cost_usd,
            "status": status,
            "finish_reason": finish_reason,
            "tool_calls_count": 1 if emits_tool else 0,
            "tool_calls_summary": [{"name": _TOOLS[0], "args_preview": "{}"}] if emits_tool else [],
            "error_code": error_code,
            "error_message": error_message,
            "input_preview": f"synthetic prompt {i}",
            "output_preview": output_preview,
            "metadata": {"synthetic": True},
        }
        sdk.transport.submit(inference_event)
        inference_count += 1

        if emits_tool:
            tool_name = random.choice(_TOOLS)
            tool_started = started + timedelta(milliseconds=ttft_ms)
            tool_latency = random.randint(10, 500)
            tool_finished = tool_started + timedelta(milliseconds=tool_latency)
            tool_event = {
                "schema_version": SCHEMA_VERSION,
                "log_type": LogType.TOOL_EXECUTION.value,
                "service": "synthetic",
                "sdk_version": "0.1.0",
                "request_id": str(uuid.uuid4()),
                "tool_call_id": str(uuid.uuid4()),
                "parent_inference_request_id": str(request_id),
                "conversation_id": str(conv_id),
                "session_id": str(sess_id),
                "user_id": None,
                "tool_name": tool_name,
                "started_at": tool_started.isoformat(),
                "finished_at": tool_finished.isoformat(),
                "latency_ms": tool_latency,
                "status": "ok",
                "error_code": None,
                "error_message": None,
                "args_preview": '{"q":"synthetic"}',
                "result_preview": f"synthetic-result-for-{tool_name}",
                "result_size_bytes": len(f"synthetic-result-for-{tool_name}"),
                "metadata": {"synthetic": True},
            }
            sdk.transport.submit(tool_event)
            tool_count += 1

    return {
        "inference_events": inference_count,
        "tool_events": tool_count,
        "error_events": error_count,
    }
