"""LangChain agent traced via chatbot-sdk's SDKCallback.

Run with:
    CHATBOT_SDK_URL=... CHATBOT_SDK_KEY=... OPENAI_API_KEY=... python langchain_agent.py
"""
import asyncio
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from chatbot_sdk import InferenceLogger
from chatbot_sdk.integrations.langchain import SDKCallback


async def main() -> None:
    logger = InferenceLogger.from_env()
    llm = ChatOpenAI(model="gpt-4o-mini")
    callback = SDKCallback(sdk=logger)
    async with logger:
        async with logger.context(conversation_id=uuid4()):
            result = await llm.ainvoke(
                [HumanMessage(content="Say hello in 5 words.")],
                config={"callbacks": [callback]},
            )
            print(result.content)


if __name__ == "__main__":
    asyncio.run(main())
