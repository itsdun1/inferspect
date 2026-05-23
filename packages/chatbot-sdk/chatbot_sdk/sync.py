"""Sync facade for ``InferenceLogger``.

Customers who aren't in an asyncio event loop (Flask, Django sync views,
scripts, notebooks) get the same API surface as :class:`InferenceLogger` but
with sync callables. Implementation: spin up a daemon thread running an
asyncio loop and drive the async logger from it via
``asyncio.run_coroutine_threadsafe``.

Usage::

    from chatbot_sdk import SyncInferenceLogger

    logger = SyncInferenceLogger.from_env()
    with logger:
        with logger.inference(provider="openai", model="gpt-4o-mini") as span:
            resp = client.chat.completions.create(...)
            span.set_response(resp)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from collections.abc import Iterator
from typing import Any
from uuid import UUID

from chatbot_sdk.client import InferenceLogger, InferenceSpan, ToolSpan
from chatbot_sdk.transport import BatchedLogTransport


class SyncInferenceLogger:
    """Sync wrapper around :class:`InferenceLogger`. Same constructor args."""

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
        self._async = InferenceLogger(
            ingestion_url=ingestion_url,
            service=service,
            sdk_version=sdk_version,
            api_key=api_key,
            pii_redact=pii_redact,
            pii_recognizers=pii_recognizers,
            transport=transport,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = False

    @classmethod
    def from_env(cls) -> SyncInferenceLogger:
        """Build from ``CHATBOT_SDK_URL`` / ``CHATBOT_SDK_KEY`` /
        ``CHATBOT_SDK_SERVICE``. Raises ``ValueError`` if URL is missing."""
        url = os.environ.get("CHATBOT_SDK_URL")
        if not url:
            raise ValueError(
                "CHATBOT_SDK_URL is not set; cannot build SyncInferenceLogger.from_env()"
            )
        api_key = os.environ.get("CHATBOT_SDK_KEY")
        service = os.environ.get("CHATBOT_SDK_SERVICE", "app")
        return cls(ingestion_url=url, service=service, api_key=api_key)

    # ─── lifecycle ───────────────────────────────────────────────
    def start(self) -> None:
        if self._started:
            return
        ready = threading.Event()

        def _run() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            ready.set()
            try:
                loop.run_forever()
            finally:
                loop.close()

        self._thread = threading.Thread(
            target=_run, name="chatbot-sdk-sync-loop", daemon=True
        )
        self._thread.start()
        ready.wait()
        assert self._loop is not None
        self._submit_coro(self._async.start())
        self._started = True

    def close(self) -> None:
        if not self._started:
            return
        try:
            self._submit_coro(self._async.close())
        finally:
            assert self._loop is not None
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=5.0)
            self._loop = None
            self._thread = None
            self._started = False

    def __enter__(self) -> SyncInferenceLogger:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ─── span context managers (sync) ────────────────────────────
    @contextlib.contextmanager
    def inference(self, **kwargs: Any) -> Iterator[InferenceSpan]:
        """Sync mirror of :meth:`InferenceLogger.inference`. Drives the async
        context manager from the daemon loop."""
        cm = self._async.inference(**kwargs)
        span = self._submit_coro(cm.__aenter__())
        try:
            yield span
        except BaseException as exc:
            # Pump the exception into the async ctxmgr so it sees the same
            # status transitions (CANCELLED / TIMEOUT / ERROR).
            if not self._submit_coro(
                cm.__aexit__(type(exc), exc, exc.__traceback__)
            ):
                raise
        else:
            self._submit_coro(cm.__aexit__(None, None, None))

    @contextlib.contextmanager
    def tool_call(self, **kwargs: Any) -> Iterator[ToolSpan]:
        """Sync mirror of :meth:`InferenceLogger.tool_call`."""
        cm = self._async.tool_call(**kwargs)
        span = self._submit_coro(cm.__aenter__())
        try:
            yield span
        except BaseException as exc:
            if not self._submit_coro(
                cm.__aexit__(type(exc), exc, exc.__traceback__)
            ):
                raise
        else:
            self._submit_coro(cm.__aexit__(None, None, None))

    @contextlib.contextmanager
    def context(
        self,
        *,
        conversation_id: UUID | None = None,
        session_id: UUID | None = None,
        user_id: UUID | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Sync mirror of :meth:`InferenceLogger.context`. Sets the
        contextvar on the calling thread (not the daemon loop) because
        integration auto-instrumentation reads it from the caller."""
        from chatbot_sdk.client import _current_context

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

    # ─── diagnostics ─────────────────────────────────────────────
    def stats(self) -> dict[str, int]:
        return self._async.stats()

    # ─── internals ───────────────────────────────────────────────
    def _submit_coro(self, coro: Any) -> Any:
        assert self._loop is not None, "SyncInferenceLogger must be started"
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()
