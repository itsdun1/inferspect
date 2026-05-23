"""Smoke tests for the LangChain SDKCallback integration.

We don't actually drive a LangChain agent — we just call the callback
methods directly with the same shapes LangChain would and assert that one
inference event lands on the SDK transport."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from chatbot_sdk.client import InferenceLogger
from chatbot_sdk.integrations.langchain import SDKCallback
from chatbot_sdk.schema import LogType, Status
from chatbot_sdk.transport import BatchedLogTransport


class _RecordingTransport(BatchedLogTransport):
    def __init__(self) -> None:
        super().__init__(
            ingestion_url="http://test.invalid",
            service="test",
            sdk_version="test",
        )
        self.events: list[dict] = []

    def submit(self, event: dict) -> None:  # type: ignore[override]
        self.events.append(event)

    async def start(self) -> None:  # type: ignore[override]
        pass

    async def close(self) -> None:  # type: ignore[override]
        pass


@pytest.fixture
def logger():
    return InferenceLogger(
        ingestion_url="http://test.invalid",
        service="chat-service",
        transport=_RecordingTransport(),
    )


async def test_sdk_callback_emits_one_inference_event(logger):
    """One LLM start → chunk → end produces exactly one inference log."""
    callback = SDKCallback(sdk=logger, conversation_id=uuid.uuid4(), user_id=uuid.uuid4())
    run_id = uuid.uuid4()

    # Simulate a chat-model start. LangChain passes serialized + nested messages.
    human = SimpleNamespace(type="human", content="hello there")
    await callback.on_chat_model_start(
        serialized={"kwargs": {"model": "gpt-4o-mini"}},
        messages=[[human]],
        run_id=run_id,
    )
    # One streaming chunk.
    chunk = SimpleNamespace(
        content="hi",
        usage_metadata={"input_tokens": 3, "output_tokens": 1},
        tool_call_chunks=None,
    )
    await callback.on_llm_new_token("hi", chunk=chunk, run_id=run_id)
    # End — pass an LLMResult-shaped response with one generation.
    final_msg = SimpleNamespace(
        content="hi back",
        usage_metadata={"input_tokens": 3, "output_tokens": 2},
        tool_calls=[],
    )
    response = SimpleNamespace(
        generations=[[SimpleNamespace(message=final_msg, text="hi back")]],
    )
    await callback.on_llm_end(response, run_id=run_id)

    events = logger.transport.events  # type: ignore[attr-defined]
    assert len(events) == 1
    e = events[0]
    assert e["log_type"] == LogType.INFERENCE
    assert e["status"] == Status.OK
    assert e["provider"] == "openai"
    assert e["model"] == "gpt-4o-mini"
    # output_preview = concatenation of streamed chunk text + final set_response text.
    assert "hi back" in e["output_preview"]
    assert callback.last_inference_request_id is not None


async def test_sdk_callback_reads_context_when_ids_not_given(logger):
    """Without explicit conversation_id, the callback picks it from current_context()."""
    callback = SDKCallback(sdk=logger)  # no ids
    run_id = uuid.uuid4()
    conv = uuid.uuid4()

    async with logger.context(conversation_id=conv):
        await callback.on_chat_model_start(
            serialized={"kwargs": {"model": "claude-3-5-sonnet"}},
            messages=[[SimpleNamespace(type="human", content="q")]],
            run_id=run_id,
        )
        await callback.on_llm_end(
            SimpleNamespace(
                generations=[[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="a",
                            usage_metadata={"input_tokens": 1, "output_tokens": 1},
                            tool_calls=[],
                        ),
                        text="a",
                    )
                ]]
            ),
            run_id=run_id,
        )

    events = logger.transport.events  # type: ignore[attr-defined]
    assert len(events) == 1
    assert events[0]["conversation_id"] == str(conv)
    assert events[0]["provider"] == "anthropic"


async def test_sdk_callback_tool_lifecycle(logger):
    callback = SDKCallback(sdk=logger, conversation_id=uuid.uuid4())
    run_id = uuid.uuid4()
    await callback.on_tool_start(
        serialized={"name": "search"},
        input_str="",
        inputs={"q": "cats"},
        run_id=run_id,
    )
    await callback.on_tool_end({"results": ["cat1"]}, run_id=run_id)

    events = logger.transport.events  # type: ignore[attr-defined]
    assert len(events) == 1
    assert events[0]["log_type"] == LogType.TOOL_EXECUTION
    assert events[0]["tool_name"] == "search"
    assert events[0]["status"] == Status.OK


async def test_sdk_callback_on_llm_error_records_error(logger):
    callback = SDKCallback(sdk=logger, conversation_id=uuid.uuid4())
    run_id = uuid.uuid4()
    await callback.on_chat_model_start(
        serialized={"kwargs": {"model": "gpt-4o-mini"}},
        messages=[[SimpleNamespace(type="human", content="q")]],
        run_id=run_id,
    )
    await callback.on_llm_error(RuntimeError("upstream 500"), run_id=run_id)

    events = logger.transport.events  # type: ignore[attr-defined]
    assert len(events) == 1
    assert events[0]["status"] == Status.ERROR
    assert events[0]["error_code"] == "RuntimeError"
