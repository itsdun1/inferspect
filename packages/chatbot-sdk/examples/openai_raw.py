"""Raw OpenAI client, auto-traced via chatbot-sdk.

Run with:
    CHATBOT_SDK_URL=... CHATBOT_SDK_KEY=... OPENAI_API_KEY=... python openai_raw.py
"""
import asyncio
from uuid import uuid4

from openai import AsyncOpenAI

from chatbot_sdk import InferenceLogger
from chatbot_sdk.integrations.openai import instrument


async def main() -> None:
    client = AsyncOpenAI()
    logger = InferenceLogger.from_env()
    instrument(client, logger=logger)
    async with logger:
        async with logger.context(conversation_id=uuid4()):
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "Say hello in 5 words."}],
            )
            print(resp.choices[0].message.content)


if __name__ == "__main__":
    asyncio.run(main())
