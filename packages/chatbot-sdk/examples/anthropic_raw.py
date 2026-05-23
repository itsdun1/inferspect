"""Raw Anthropic client, auto-traced via chatbot-sdk.

Run with:
    CHATBOT_SDK_URL=... CHATBOT_SDK_KEY=... ANTHROPIC_API_KEY=... python anthropic_raw.py
"""
import asyncio
from uuid import uuid4

from anthropic import AsyncAnthropic

from chatbot_sdk import InferenceLogger
from chatbot_sdk.integrations.anthropic import instrument


async def main() -> None:
    client = AsyncAnthropic()
    logger = InferenceLogger.from_env()
    instrument(client, logger=logger)
    async with logger:
        async with logger.context(conversation_id=uuid4()):
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": "Say hello in 5 words."}],
            )
            print(resp.content[0].text)


if __name__ == "__main__":
    asyncio.run(main())
