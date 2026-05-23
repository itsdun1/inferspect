"""Sync OpenAI inside a Flask route — manual span (the documented sync pattern).

``instrument()`` is async-only; for sync code use SyncInferenceLogger with
``with logger.inference(...)`` around each call.

Run with:
    CHATBOT_SDK_URL=... CHATBOT_SDK_KEY=... OPENAI_API_KEY=... flask --app sync_flask run
"""
from flask import Flask, request
from openai import OpenAI

from chatbot_sdk import SyncInferenceLogger

app = Flask(__name__)
client = OpenAI()
sdk = SyncInferenceLogger.from_env()
sdk.start()


@app.post("/chat")
def chat() -> dict:
    msg = request.json["message"]
    with sdk.inference(provider="openai", model="gpt-4o-mini", input_preview=msg) as span:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": msg}],
        )
        span.set_output_preview(resp.choices[0].message.content or "")
        return {"reply": resp.choices[0].message.content}
