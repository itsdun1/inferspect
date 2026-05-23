"""Custom in-house LLM, AUTO-instrumented via your own integration shim.

When ``inferspect-sdk`` ships an integration for your library
(OpenAI / Anthropic / Gemini / LangChain / LlamaIndex), you import its
``instrument()`` and you're done — one line at boot, every call auto-traced.

When it doesn't, you have two options:

  Tier 2 (see custom_llm.py): wrap every call site with
          ``async with logger.inference(...) as span: ...``.
          Cheap if you have 1-3 call sites.

  Tier 3 (THIS FILE): write your own ``instrument()`` function once,
          monkey-patching your client's method via the SDK's public
          ``wrap_method()`` helper. After boot, every call to that method
          is auto-traced — same ergonomics as the built-in integrations.

You write ~25 lines once. Every subsequent call to your client is auto-traced
with zero per-call code.

What you MUST do to make this work — read the comments tagged ★ below.

Run with::

    CHATBOT_SDK_URL=https://your-ingestion/v1/logs \\
    CHATBOT_SDK_KEY=osk_... \\
    python custom_llm_auto.py
"""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

from chatbot_sdk import InferenceLogger
from chatbot_sdk.integrations._instrument import current_ctx_kwargs, wrap_method


# ─── Pretend "in-house" LLM client (same as custom_llm.py) ────────────

class CorpLLM:
    """Hypothetical in-house LLM with its own response shape."""

    async def generate(self, *, model: str, prompt: str) -> dict:
        await asyncio.sleep(0.4)
        return {
            "reply_text": f"You said: {prompt!r}. Here is a thoughtful answer.",
            "function_calls": [],
            "metrics": {"tokens_consumed_in": 12, "tokens_consumed_out": 18},
        }


# ─── ★ YOUR OWN instrument() FUNCTION — write once, in your own repo ──
#
# This file lives in YOUR codebase, NOT in inferspect-sdk. The SDK exposes
# the helpers (``wrap_method``, ``current_ctx_kwargs``, ``InferenceLogger``)
# that you assemble into your own integration. You're following the same
# contract the built-in integrations follow.
#
# What you HAVE to do — five steps, one each marked ★:

def instrument(client: "CorpLLM", *, logger: InferenceLogger) -> None:
    """Monkey-patch CorpLLM.generate so every call is auto-traced.

    Idempotent — re-instrumenting the same client is a no-op (wrap_method
    tags the wrapped method with a sentinel so future instrument() calls
    notice and bail out)."""

    # ★ STEP 1 — define a "factory" that builds your wrapper from the
    #            original method. Closures over `original` and `logger`.
    def _factory(original):

        # ★ STEP 2 — write an async wrapper with the SAME shape as the
        #            method you're replacing. ``client.generate(model=..., prompt=...)``
        #            is async and takes kwargs, so wrapper does too.
        async def wrapper(*args, **kwargs):

            # ★ STEP 3 — open an inference span via ``logger.inference(...)``.
            #            Fill in provider/model from kwargs. ``current_ctx_kwargs()``
            #            picks up conversation_id / session_id / user_id from
            #            ``logger.context(...)`` if the caller set them.
            async with logger.inference(
                provider="corp-internal",
                model=kwargs.get("model", "unknown"),
                input_preview=kwargs.get("prompt", "")[:500],
                **current_ctx_kwargs(),
            ) as span:

                # ★ STEP 4 — call the ORIGINAL method. The SDK is paused
                #            here; we let the real LLM call happen.
                resp = await original(*args, **kwargs)

                # ★ STEP 5 — translate YOUR response shape into the SDK's
                #            neutral ``SimpleNamespace`` contract:
                #              .content        → string the model said
                #              .tool_calls     → list of {"name", "args"} dicts
                #              .usage_metadata → dict with "input_tokens", "output_tokens"
                #            Feed it via ``span.set_response()``. The SDK then
                #            handles PII, latency, errors, transport.
                span.set_response(SimpleNamespace(
                    content=resp["reply_text"],
                    tool_calls=[],
                    usage_metadata={
                        "input_tokens":  resp["metrics"]["tokens_consumed_in"],
                        "output_tokens": resp["metrics"]["tokens_consumed_out"],
                    },
                ))
                return resp                              # ← give the customer their original response back

        return wrapper

    # ``wrap_method`` walks the dotted attribute path on ``client``, swaps
    # the leaf for our wrapper, and tags it so this is idempotent. For a
    # nested method you'd pass e.g. "chat.completions.create".
    wrap_method(client, "generate", _factory)


# ─── Customer's app code AFTER instrument() — pure CorpLLM ────────────
# Nothing in here mentions the SDK. Once instrument() has run, every
# generate() call is invisibly auto-traced.

async def handle_chat(client: CorpLLM, prompt: str) -> str:
    resp = await client.generate(model="internal-llama", prompt=prompt)
    return resp["reply_text"]


# ─── Boot + demo ──────────────────────────────────────────────────────

async def main() -> None:
    client = CorpLLM()
    logger = InferenceLogger.from_env()
    instrument(client, logger=logger)                   # ← the only line that mentions the SDK

    async with logger:
        async with logger.context(conversation_id=uuid4()):
            # Pure CorpLLM calls — auto-traced behind the scenes.
            reply1 = await handle_chat(client, "What's your refund policy?")
            reply2 = await handle_chat(client, "What does AI liability cover?")
            print("Reply 1:", reply1)
            print("Reply 2:", reply2)
            # Two inference_logs rows landed in ClickHouse, tagged
            # provider="corp-internal", model="internal-llama", with the
            # right tokens / latency / PII-redacted previews.


if __name__ == "__main__":
    asyncio.run(main())
