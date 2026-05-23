"""Raw google-genai client, auto-traced via chatbot-sdk.

Run with:
    CHATBOT_SDK_URL=... CHATBOT_SDK_KEY=... GOOGLE_API_KEY=... python gemini_raw.py
"""
import asyncio
from uuid import uuid4

from google import genai

from chatbot_sdk import InferenceLogger
from chatbot_sdk.integrations.google import instrument


async def main() -> None:
    client = genai.Client()
    logger = InferenceLogger.from_env()
    instrument(client, logger=logger)
    async with logger:
        async with logger.context(conversation_id=uuid4()):
            resp = await client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents="Say hello in 5 words.",
            )
            print(resp.text)


if __name__ == "__main__":
    asyncio.run(main())
