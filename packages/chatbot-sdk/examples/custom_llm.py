"""Custom in-house LLM client, manually instrumented.

Use this pattern when ``inferspect-sdk`` doesn't ship a built-in
integration for your LLM library (e.g. an internal model gateway, a less
common provider like Mistral / Cohere / xAI, or a hand-rolled HTTP client).

The escape hatch: wrap the call site in ``async with logger.inference(...)``
and feed the response into the span yourself via the neutral SimpleNamespace
shape — ``.content``, ``.tool_calls``, ``.usage_metadata``. The SDK handles
everything else (timing, PII redaction, transport, ClickHouse ingestion).

Run with::

    CHATBOT_SDK_URL=https://your-ingestion/v1/logs \\
    CHATBOT_SDK_KEY=osk_... \\
    python custom_llm.py
"""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx

from chatbot_sdk import InferenceLogger


# ─── Pretend "in-house" LLM client ────────────────────────────────────
# Stand-in for whatever your team's internal gateway looks like. The shape
# of the response is intentionally NOT OpenAI-compatible — that's the
# point of this example. Your real client might return a completely
# different dict / dataclass / Pydantic model.

class CorpLLM:
    """Hypothetical in-house LLM with its own response shape."""

    def __init__(self, base_url: str) -> None:
        self._base = base_url
        self._http = httpx.AsyncClient(timeout=30.0)

    async def generate(self, *, model: str, prompt: str) -> dict:
        """Returns the corp-internal response shape — NOT OpenAI's."""
        # In the demo we mock this. In real life it'd be:
        #     resp = await self._http.post(f"{self._base}/v1/generate", json={...})
        await asyncio.sleep(0.4)  # simulate model latency
        return {
            "request_id": "corp_req_abc123",
            "model_name": model,
            "reply_text": f"You said: {prompt!r}. Here is a deeply thoughtful answer.",
            "function_calls": [],   # this corp LLM doesn't do tool calls in this example
            "metrics": {
                "tokens_consumed_in": 12,
                "tokens_consumed_out": 18,
                "compute_ms": 412,
            },
        }


# ─── Customer's adapter — corp shape → SDK's neutral shape ─────────────
# Any custom LLM customer writes this translation function once. It hands
# the SDK a SimpleNamespace with the three attributes set_response wants:
#   .content         → the model's reply text (string)
#   .tool_calls      → list of {"name": str, "args": dict}
#   .usage_metadata  → dict with "input_tokens", "output_tokens"

def _corp_to_sdk_shape(resp: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=resp.get("reply_text", ""),
        tool_calls=[
            {"name": fc.get("fn", ""), "args": fc.get("params", {})}
            for fc in (resp.get("function_calls") or [])
        ],
        usage_metadata={
            "input_tokens":  int(resp.get("metrics", {}).get("tokens_consumed_in",  0)),
            "output_tokens": int(resp.get("metrics", {}).get("tokens_consumed_out", 0)),
        },
    )


# ─── Customer's request handler — what they call per request ───────────

async def handle_chat(prompt: str, *, conversation_id, user_id, logger):
    """One LLM call, manually traced via the SDK's escape-hatch pattern."""
    client = CorpLLM(base_url="https://internal-llm.corp")

    # The 4 SDK lines for the manual path. Identical shape to a LangChain
    # callback or an auto-instrumented OpenAI call — same span lifecycle,
    # same PII redaction, same log shipping. Just driven by the customer
    # instead of by instrument()/SDKCallback.
    async with logger.inference(
        provider="corp-internal",
        model="internal-llama-2-fine-tune",
        conversation_id=conversation_id,
        user_id=user_id,
        input_preview=prompt,
    ) as span:
        resp = await client.generate(model="internal-llama-2-fine-tune", prompt=prompt)
        span.set_response(_corp_to_sdk_shape(resp))
        return resp["reply_text"]


# ─── Boot + demo ──────────────────────────────────────────────────────

async def main() -> None:
    logger = InferenceLogger.from_env()
    async with logger:
        reply = await handle_chat(
            "What's your refund policy?",
            conversation_id=uuid4(),
            user_id=uuid4(),
            logger=logger,
        )
        print("Customer's app got back:", reply)
        # Meanwhile, the SDK has shipped one inference_logs row to ingestion
        # with the right provider/model/latency/tokens/redacted previews.


if __name__ == "__main__":
    asyncio.run(main())
