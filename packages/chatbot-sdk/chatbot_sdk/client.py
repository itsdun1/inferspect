"""InferenceLogger — the SDK's public entry point.

Provides context managers and decorators to capture LLM inference calls and
tool executions. Logs are buffered and shipped asynchronously by
``BatchedLogTransport``.

Typical usage::

    logger = InferenceLogger(ingestion_url=..., service="chat-service")
    await logger.start()

    async with logger.inference(
        conversation_id=conv_id,
        provider="google",
        model="gemini-2.5-pro",
    ) as span:
        chunks = []
        async for chunk in llm.astream(messages):
            span.observe_chunk(chunk)
            chunks.append(chunk)
        span.set_output_preview("".join(c.content for c in chunks))

    @logger.tool_traced(name="get_current_time")
    async def get_current_time(timezone: str = "UTC") -> str:
        ...
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from typing import Any, ParamSpec, TypeVar
from uuid import UUID, uuid4

from chatbot_sdk.schema import (
    SCHEMA_VERSION,
    FinishReason,
    InferenceLog,
    Status,
    ToolCallSummary,
    ToolExecutionLog,
)
from chatbot_sdk.transport import BatchedLogTransport

PREVIEW_MAX = 500
P = ParamSpec("P")
R = TypeVar("R")

# Per-request context set by ``InferenceLogger.context(...)`` and read by
# integration ``instrument()`` helpers so request-scoped ids
# (conversation_id, session_id, user_id) flow into auto-traced spans without
# the caller threading them through each LLM call.
# Default is None (sentinel for "no active context"); ``current_context()``
# returns ``{}`` so callers always see a dict. We avoid a mutable default for
# the ContextVar itself per ruff B039.
_current_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "chatbot_sdk_current_context", default=None
)


def current_context() -> dict[str, Any]:
    """Return the active per-request context dict (or empty dict)."""
    return _current_context.get() or {}


def _now() -> datetime:
    return datetime.now(UTC)


def _truncate(text: str, limit: int = PREVIEW_MAX) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + "…"


def _ms_between(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() * 1000))


class InferenceSpan:
    """Mutable handle for an in-flight LLM call. Mutated by user code, finalized
    on context exit. ``observe_chunk`` is the streaming hook."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        service: str,
        request_id: UUID,
        conversation_id: UUID | None,
        session_id: UUID | None,
        user_id: UUID | None,
        stream: bool,
        input_preview: str,
        metadata: dict[str, Any],
        sdk: InferenceLogger | None = None,
    ) -> None:
        self.request_id = request_id
        self._sdk = sdk
        self._provider = provider
        self._model = model
        self._service = service
        self._conversation_id = conversation_id
        self._session_id = session_id
        self._user_id = user_id
        self._stream = stream
        self._input_preview = _truncate(input_preview)
        self._metadata = metadata or {}

        self._started_at = _now()
        self._started_perf = time.perf_counter()
        self._ttft_ms: int | None = None
        self._output_chunks: list[str] = []
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._cost_usd = 0.0
        self._finish_reason: FinishReason | None = None
        self._tool_calls: list[ToolCallSummary] = []

    # ─── Hooks for the caller ────────────────────────────────────
    def observe_chunk(self, chunk: Any) -> None:
        """Call once per streaming chunk. Captures ttft on first call and
        accumulates incremental content if available."""
        if self._ttft_ms is None:
            self._ttft_ms = int((time.perf_counter() - self._started_perf) * 1000)
        text = _chunk_text(chunk)
        if text:
            self._output_chunks.append(text)
        # LangChain final chunks usually carry usage_metadata.
        meta = getattr(chunk, "usage_metadata", None)
        if meta:
            self._prompt_tokens = int(meta.get("input_tokens", self._prompt_tokens) or self._prompt_tokens)
            self._completion_tokens = int(
                meta.get("output_tokens", self._completion_tokens) or self._completion_tokens
            )
        # Tool calls: LangChain attaches tool_call_chunks on AIMessageChunk.
        tool_chunks = getattr(chunk, "tool_call_chunks", None)
        if tool_chunks:
            for tc in tool_chunks:
                name = tc.get("name") or ""
                if name and not any(t.name == name for t in self._tool_calls):
                    self._tool_calls.append(
                        ToolCallSummary(name=name, args_preview=_truncate(str(tc.get("args", ""))))
                    )

    def set_response(self, response: Any) -> None:
        """Non-streaming path: call with the final response message."""
        text = _chunk_text(response)
        if text:
            self._output_chunks.append(text)
        meta = getattr(response, "usage_metadata", None) or {}
        self._prompt_tokens = int(meta.get("input_tokens", self._prompt_tokens) or self._prompt_tokens)
        self._completion_tokens = int(
            meta.get("output_tokens", self._completion_tokens) or self._completion_tokens
        )
        tool_calls = getattr(response, "tool_calls", None)
        if tool_calls:
            for tc in tool_calls:
                self._tool_calls.append(
                    ToolCallSummary(
                        name=tc.get("name", ""),
                        args_preview=_truncate(json.dumps(tc.get("args", {}), default=str)),
                    )
                )

    def set_output_preview(self, text: str) -> None:
        self._output_chunks = [text]

    def set_tokens(self, prompt: int | None = None, completion: int | None = None) -> None:
        if prompt is not None:
            self._prompt_tokens = prompt
        if completion is not None:
            self._completion_tokens = completion

    def set_cost(self, usd: float) -> None:
        self._cost_usd = usd

    def set_finish_reason(self, reason: FinishReason | str) -> None:
        self._finish_reason = FinishReason(reason) if isinstance(reason, str) else reason

    def set_metadata(self, **kwargs: Any) -> None:
        self._metadata.update(kwargs)

    # ─── Internals used by InferenceLogger ───────────────────────
    def _finalize(self, status: Status, error: BaseException | None) -> InferenceLog:
        # Measure how long this _finalize itself takes (PII redaction +
        # Pydantic construction). Integration wrappers also record pre-call
        # SDK time via set_metadata("sdk_pre_call_ms", ...). We sum them to
        # produce metadata["sdk_overhead_ms"] — total time the SDK added
        # outside the actual provider API call.
        t_finalize_start = time.perf_counter()

        finished_at = _now()
        total = self._prompt_tokens + self._completion_tokens
        output_preview = _truncate("".join(self._output_chunks))
        input_preview = self._input_preview
        if self._sdk is not None and self._sdk._pii_redact:
            from chatbot_sdk.pii import redact_text

            recognizers = self._sdk._pii_recognizers
            input_preview = redact_text(input_preview, recognizers)
            output_preview = redact_text(output_preview, recognizers)
        finish = self._finish_reason or (
            FinishReason.TOOL_CALLS if self._tool_calls else
            FinishReason.CANCELLED if status == Status.CANCELLED else
            FinishReason.ERROR if status == Status.ERROR else
            FinishReason.STOP
        )

        # Stamp SDK timing into metadata BEFORE we build the Pydantic model
        # so it lands in the wire payload. Integration wrappers may have
        # already stamped sdk_pre_call_ms and api_call_ms via set_metadata.
        finalize_ms = (time.perf_counter() - t_finalize_start) * 1000
        pre_call_ms = float(self._metadata.get("sdk_pre_call_ms", 0) or 0)
        self._metadata["sdk_finalize_ms"] = round(finalize_ms, 3)
        self._metadata["sdk_overhead_ms"] = round(pre_call_ms + finalize_ms, 3)

        return InferenceLog(
            schema_version=SCHEMA_VERSION,
            service=self._service,
            request_id=self.request_id,
            conversation_id=self._conversation_id,
            session_id=self._session_id,
            user_id=self._user_id,
            provider=self._provider,
            model=self._model,
            stream=self._stream,
            started_at=self._started_at,
            finished_at=finished_at,
            latency_ms=_ms_between(self._started_at, finished_at),
            ttft_ms=self._ttft_ms,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
            total_tokens=total,
            cost_usd=self._cost_usd,
            status=status,
            finish_reason=finish,
            tool_calls_count=len(self._tool_calls),
            tool_calls_summary=self._tool_calls,
            error_code=type(error).__name__ if error else None,
            error_message=str(error)[:500] if error else None,
            input_preview=input_preview,
            output_preview=output_preview,
            metadata=self._metadata,
        )


class ToolSpan:
    def __init__(
        self,
        *,
        tool_name: str,
        service: str,
        request_id: UUID,
        tool_call_id: UUID,
        parent_inference_request_id: UUID | None,
        conversation_id: UUID | None,
        session_id: UUID | None,
        user_id: UUID | None,
        args_preview: str,
        metadata: dict[str, Any],
        sdk: InferenceLogger | None = None,
    ) -> None:
        self.request_id = request_id
        self.tool_call_id = tool_call_id
        self._sdk = sdk
        self._tool_name = tool_name
        self._service = service
        self._parent = parent_inference_request_id
        self._conversation_id = conversation_id
        self._session_id = session_id
        self._user_id = user_id
        self._args_preview = _truncate(args_preview)
        self._metadata = metadata or {}
        self._started_at = _now()
        self._result_preview = ""
        self._result_size_bytes = 0

    def set_result(self, result: Any) -> None:
        text = result if isinstance(result, str) else json.dumps(result, default=str)
        self._result_preview = _truncate(text)
        self._result_size_bytes = len(text.encode("utf-8")) if text else 0

    def set_metadata(self, **kwargs: Any) -> None:
        self._metadata.update(kwargs)

    def _finalize(self, status: Status, error: BaseException | None) -> ToolExecutionLog:
        finished_at = _now()
        args_preview = self._args_preview
        result_preview = self._result_preview
        if self._sdk is not None and self._sdk._pii_redact:
            from chatbot_sdk.pii import redact_text

            recognizers = self._sdk._pii_recognizers
            args_preview = redact_text(args_preview, recognizers)
            result_preview = redact_text(result_preview, recognizers)
        return ToolExecutionLog(
            schema_version=SCHEMA_VERSION,
            service=self._service,
            request_id=self.request_id,
            tool_call_id=self.tool_call_id,
            parent_inference_request_id=self._parent,
            conversation_id=self._conversation_id,
            session_id=self._session_id,
            user_id=self._user_id,
            tool_name=self._tool_name,
            started_at=self._started_at,
            finished_at=finished_at,
            latency_ms=_ms_between(self._started_at, finished_at),
            status=status,
            error_code=type(error).__name__ if error else None,
            error_message=str(error)[:500] if error else None,
            args_preview=args_preview,
            result_preview=result_preview,
            result_size_bytes=self._result_size_bytes,
            metadata=self._metadata,
        )


class InferenceLogger:
    """User-facing entry point. Wraps a BatchedLogTransport and provides
    span context managers / decorators."""

    def __init__(
        self,
        *,
        ingestion_url: str,
        service: str,
        sdk_version: str = "0.2.0",
        api_key: str | None = None,
        pii_redact: bool = True,
        pii_recognizers: list[str] | None = None,
        transport: BatchedLogTransport | None = None,
    ) -> None:
        self.service = service
        self.sdk_version = sdk_version
        self._pii_redact = pii_redact
        self._pii_recognizers = pii_recognizers
        self.transport = transport or BatchedLogTransport(
            ingestion_url=ingestion_url,
            service=service,
            sdk_version=sdk_version,
            api_key=api_key,
        )

    @classmethod
    def from_env(cls) -> InferenceLogger:
        """Build a logger from ``CHATBOT_SDK_URL`` / ``CHATBOT_SDK_KEY`` /
        ``CHATBOT_SDK_SERVICE`` (service defaults to ``"app"``).

        Raises ``ValueError`` if ``CHATBOT_SDK_URL`` isn't set."""
        url = os.environ.get("CHATBOT_SDK_URL")
        if not url:
            raise ValueError(
                "CHATBOT_SDK_URL is not set; cannot build InferenceLogger.from_env()"
            )
        api_key = os.environ.get("CHATBOT_SDK_KEY")
        service = os.environ.get("CHATBOT_SDK_SERVICE", "app")
        return cls(ingestion_url=url, service=service, api_key=api_key)

    async def start(self) -> None:
        await self.transport.start()

    async def close(self) -> None:
        await self.transport.close()

    async def __aenter__(self) -> InferenceLogger:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @contextlib.asynccontextmanager
    async def context(
        self,
        *,
        conversation_id: UUID | None = None,
        session_id: UUID | None = None,
        user_id: UUID | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Set a per-request ContextVar that integration ``instrument()`` helpers
        read so they can stamp request-scoped ids onto spans without the caller
        threading them through every LLM call."""
        ctx: dict[str, Any] = {
            "conversation_id": conversation_id,
            "session_id": session_id,
            "user_id": user_id,
        }
        token = _current_context.set(ctx)
        try:
            yield ctx
        finally:
            _current_context.reset(token)

    # ─── Inference span ──────────────────────────────────────────
    @contextlib.asynccontextmanager
    async def inference(
        self,
        *,
        provider: str,
        model: str,
        conversation_id: UUID | None = None,
        session_id: UUID | None = None,
        user_id: UUID | None = None,
        stream: bool = False,
        input_preview: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[InferenceSpan]:
        span = InferenceSpan(
            provider=provider,
            model=model,
            service=self.service,
            request_id=uuid4(),
            conversation_id=conversation_id,
            session_id=session_id,
            user_id=user_id,
            stream=stream,
            input_preview=input_preview,
            metadata=metadata or {},
            sdk=self,
        )
        status = Status.OK
        error: BaseException | None = None
        try:
            yield span
        except (KeyboardInterrupt, GeneratorExit):
            raise
        except BaseException as exc:  # noqa: BLE001 — we always re-raise
            import asyncio

            if isinstance(exc, asyncio.CancelledError):
                status = Status.CANCELLED
            elif isinstance(exc, asyncio.TimeoutError):
                status = Status.TIMEOUT
            else:
                status = Status.ERROR
            error = exc
            raise
        finally:
            log = span._finalize(status, error)
            self.transport.submit(log.model_dump(mode="json"))

    # ─── Tool span ───────────────────────────────────────────────
    @contextlib.asynccontextmanager
    async def tool_call(
        self,
        *,
        tool_name: str,
        tool_call_id: UUID | None = None,
        parent_inference_request_id: UUID | None = None,
        conversation_id: UUID | None = None,
        session_id: UUID | None = None,
        user_id: UUID | None = None,
        args_preview: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[ToolSpan]:
        span = ToolSpan(
            tool_name=tool_name,
            service=self.service,
            request_id=uuid4(),
            tool_call_id=tool_call_id or uuid4(),
            parent_inference_request_id=parent_inference_request_id,
            conversation_id=conversation_id,
            session_id=session_id,
            user_id=user_id,
            args_preview=args_preview,
            metadata=metadata or {},
            sdk=self,
        )
        status = Status.OK
        error: BaseException | None = None
        try:
            yield span
        except (KeyboardInterrupt, GeneratorExit):
            raise
        except BaseException as exc:
            import asyncio

            if isinstance(exc, asyncio.CancelledError):
                status = Status.CANCELLED
            elif isinstance(exc, asyncio.TimeoutError):
                status = Status.TIMEOUT
            else:
                status = Status.ERROR
            error = exc
            raise
        finally:
            log = span._finalize(status, error)
            self.transport.submit(log.model_dump(mode="json"))

    # ─── Decorator sugar ─────────────────────────────────────────
    def tool_traced(
        self,
        name: str | None = None,
        *,
        conversation_id_arg: str | None = None,
    ) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        """Decorator that wraps a tool function so each call emits a
        ``tool_execution`` log.

        ``conversation_id_arg``: optional name of a keyword argument from which
        to pull the conversation id (so insights can stitch the timeline)."""

        def decorate(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
            tool_name = name or fn.__name__

            @functools.wraps(fn)
            async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                conv = kwargs.get(conversation_id_arg) if conversation_id_arg else None
                args_preview = _truncate(json.dumps({"args": list(args), "kwargs": dict(kwargs)}, default=str))
                async with self.tool_call(
                    tool_name=tool_name,
                    conversation_id=conv if isinstance(conv, UUID) else None,
                    args_preview=args_preview,
                ) as span:
                    result = await fn(*args, **kwargs)
                    span.set_result(result)
                    return result

            return wrapper

        return decorate

    # ─── Diagnostics ─────────────────────────────────────────────
    def stats(self) -> dict[str, int]:
        return self.transport.stats()


# ─── helpers ─────────────────────────────────────────────────────
def _chunk_text(obj: Any) -> str:
    """Extract textual content from a LangChain chunk-shaped object.

    Handles:
      - plain str
      - AIMessage / AIMessageChunk (``.content`` as str or list of parts)
      - GenerationChunk / ChatGenerationChunk (``.text`` and/or ``.message.content``)

    Returns ``""`` for unknown shapes — we never want the chunk's ``repr()``
    leaking into the captured output (that's a real bug we caught in prod)."""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    # AIMessage / AIMessageChunk path.
    content = getattr(obj, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
        return "".join(parts)
    # GenerationChunk path — has ``.text`` directly + a nested ``.message``.
    text = getattr(obj, "text", None)
    if isinstance(text, str):
        return text
    message = getattr(obj, "message", None)
    if message is not None and message is not obj:
        return _chunk_text(message)
    return ""
