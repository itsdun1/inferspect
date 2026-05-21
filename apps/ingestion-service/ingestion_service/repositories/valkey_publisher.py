"""Repository: publish log events to Valkey Streams.

Wraps redis-py async client. The only layer in the service that talks to the
broker. Connection is opened once at app startup and shared by FastAPI deps.
"""

from __future__ import annotations

import json
from typing import Any

from chatbot_sdk.schema import LogType
from redis.asyncio import Redis


class ValkeyPublisher:
    """Publishes serialized log events to per-log_type streams."""

    def __init__(
        self,
        client: Redis,
        *,
        stream_inference: str,
        stream_tool_execution: str,
        stream_application: str,
        stream_maxlen: int,
    ) -> None:
        self._client = client
        self._streams: dict[LogType, str] = {
            LogType.INFERENCE: stream_inference,
            LogType.TOOL_EXECUTION: stream_tool_execution,
            LogType.APPLICATION: stream_application,
        }
        self._maxlen = stream_maxlen

    def stream_for(self, log_type: LogType) -> str:
        return self._streams[log_type]

    async def publish(self, log_type: LogType, event: dict[str, Any]) -> str:
        """Append a single event to the appropriate stream. Returns the stream id."""
        stream = self._streams[log_type]
        # XADD <stream> MAXLEN ~ <n> * field value
        message_id = await self._client.xadd(
            stream,
            {"payload": json.dumps(event, default=str)},
            maxlen=self._maxlen,
            approximate=True,
        )
        return message_id if isinstance(message_id, str) else message_id.decode()

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:  # noqa: BLE001 — healthcheck path swallows
            return False

    async def close(self) -> None:
        await self._client.aclose()
