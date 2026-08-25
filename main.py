"""
WhatsApp bot: receives client messages, replies using Muse Spark (Meta Model API).

Stack:
  - PyWa (async)      -> handles the WhatsApp Cloud API webhook + sending
  - FastAPI + uvicorn -> the web server that Meta calls
  - OpenAI client      -> points at Meta Model API (it's OpenAI-compatible)

Run:
  uvicorn main:fastapi_app --host 0.0.0.0 --port 8000 --reload
"""

import base64
import logging
import os
from typing import Any, cast

from dotenv import load_dotenv
from fastapi import FastAPI
from openai import AsyncOpenAI
from pywa_async import WhatsApp, types
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

load_dotenv(dotenv_path=".env")
logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("wa-bot")

# --- The web server Meta will call ------------------------------------------
fastapi_app = FastAPI()


# Middleware to log raw webhook POST bodies for debugging
class WebhookLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/webhook" and request.method == "POST":
            body = await request.body()
            log.debug("Raw /webhook body: %s", body.decode("utf-8", errors="replace"))
            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}
            request._receive = receive
        return await call_next(request)


fastapi_app.add_middleware(WebhookLoggingMiddleware)

# --- WhatsApp client (Cloud API) --------------------------------------------
# app_secret lets PyWa verify each webhook call really came from Meta.
_app_secret = os.getenv("WA_APP_SECRET") or None

wa = WhatsApp(
    phone_id=os.environ["WA_PHONE_ID"],          # "Phone number ID" from the dashboard
    token=os.environ["WA_TOKEN"],                # temp or permanent access token
    server=fastapi_app,                          # mount PyWa onto our FastAPI app
    webhook_endpoint="/webhook",                 # -> your callback URL is <public>/webhook
    verify_token=os.environ["WA_VERIFY_TOKEN"],  # any string; must match the dashboard
    app_secret=_app_secret,
    validate_updates=bool(_app_secret),          # signature check on if secret is set
)

# --- The brain: GPT-4o Mini via OpenAI API -----------------------------
llm = AsyncOpenAI(api_key=os.environ["MODEL_API_KEY"])
MODEL_ID = os.getenv("MODEL_ID", "gpt-4o-mini")
SYSTEM_PROMPT = (
    "You are a helpful assistant for a WhatsApp business bot. "
    "Reply warmly and briefly, and acknowledge the user message." 
)


async def generate_text_reply(user_text: str) -> str:
    """Use GPT-4o Mini to generate a WhatsApp reply for text messages."""
    resp = await llm.chat.completions.create(
        model=MODEL_ID,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        max_tokens=200,
        temperature=0.7,
    )
    return (resp.choices[0].message.content or "").strip()


async def extract_receipt_text(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """OCR a receipt/invoice image with GPT-4o Mini and return the text."""
    image_base64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime_type};base64,{image_base64}"

    prompt = (
        "You are an OCR assistant. Read this receipt/invoice and transcribe it. "
        "Return in plain text: business name; invoice number and date; each line item "
        "(quantity, description, unit price, line total); and the total. "
        "Preserve all numbers exactly as written. Mark anything unreadable as [unclear]. "
        "Keep it compact for WhatsApp."
    )

    resp = await llm.chat.completions.create(
        model=MODEL_ID,
        messages=cast(Any, [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url, "detail": "high"},
                    },
                ],
            }
        ]),
        max_tokens=600,
    )

    return (resp.choices[0].message.content or "").strip()


# --- Handle every incoming text and image message -------------------------
@wa.on_message()  # type: ignore[arg-type]  # pywa async-stub quirk; verified correct at runtime
async def handle_message(_: WhatsApp, msg: types.Message):
    log.debug("Entered handle_message handler")
    log.info("Message from %s: %s", msg.from_user.wa_id, msg.text)

    if msg.image is not None:
        try:
            image_bytes = await msg.image.get_bytes()
            reply = await extract_receipt_text(image_bytes, msg.image.mime_type or "image/jpeg")
        except Exception:
            log.exception("Image OCR failed")
            reply = "Sorry, I couldn't read that image. Please send a different picture."

        try:
            await msg.reply_text(reply)
        except Exception:
            log.exception("Failed to send image reply to WhatsApp")
        return

    if not msg.text:
        await msg.reply_text("I can only read text and image messages right now 🙂")
        return

    try:
        reply = await generate_text_reply(msg.text)
    except Exception:
        log.exception("LLM call failed")
        reply = "Sorry, I hit a snag. Please try again in a moment."

    try:
        await msg.reply_text(reply)
    except Exception:
        log.exception("Failed to send reply to WhatsApp")


# --- A simple health check you can open in a browser ------------------------
@fastapi_app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL_ID}


# --- Let `python main.py` start the server (same as the uvicorn command) -----
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:fastapi_app", host="0.0.0.0", port=8000, reload=True)