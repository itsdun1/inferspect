"""LlamaIndex query traced via chatbot-sdk's LlamaIndexCallback.

Run with:
    CHATBOT_SDK_URL=... CHATBOT_SDK_KEY=... OPENAI_API_KEY=... python llamaindex_query.py
"""
import asyncio

from llama_index.core import Settings
from llama_index.core.callbacks import CallbackManager
from llama_index.llms.openai import OpenAI

from chatbot_sdk import InferenceLogger
from chatbot_sdk.integrations.llamaindex import LlamaIndexCallback


async def main() -> None:
    logger = InferenceLogger.from_env()
    async with logger:
        callback = LlamaIndexCallback(sdk=logger)
        Settings.callback_manager = CallbackManager([callback])
        llm = OpenAI(model="gpt-4o-mini")
        resp = await llm.acomplete("Say hello in 5 words.")
        print(resp.text)


if __name__ == "__main__":
    asyncio.run(main())
