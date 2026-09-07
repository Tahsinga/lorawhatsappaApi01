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
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from openai import AsyncOpenAI
from pywa_async import WhatsApp, types
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from store import init_db, list_conversations, log_conversation

load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=True)
logging.basicConfig(
    level=logging.DEBUG,
    handlers=[
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("wa-bot")
log.setLevel(logging.DEBUG)
if not any(isinstance(handler, logging.FileHandler) for handler in log.handlers):
    log.addHandler(logging.FileHandler(Path(__file__).with_name("webhook.log"), encoding="utf-8"))


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Copy .env.example to .env and fill in your credentials."
        )
    return value

# --- The web server Meta will call ------------------------------------------
fastapi_app = FastAPI()
init_db()


# Middleware to log raw webhook POST bodies for debugging
class WebhookLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.rstrip("/") == "/webhook":
            log.info("Webhook request: %s %s?%s", request.method, request.url.path, request.url.query)
        if request.url.path.rstrip("/") == "/webhook" and request.method == "POST":
            body = await request.body()
            log.debug("Raw /webhook body: %s", body.decode("utf-8", errors="replace"))
            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}
            request._receive = receive
        response = await call_next(request)
        if request.url.path.rstrip("/") == "/webhook":
            log.info("Webhook response: %s", response.status_code)
        return response


fastapi_app.add_middleware(WebhookLoggingMiddleware)

# --- WhatsApp client (Cloud API) --------------------------------------------
# app_secret lets PyWa verify each webhook call really came from Meta.
_app_secret = os.getenv("WA_APP_SECRET") or None

wa = WhatsApp(
    phone_id=required_env("WA_PHONE_ID"),        # "Phone number ID" from the dashboard
    token=required_env("WA_TOKEN"),              # temp or permanent access token
    server=fastapi_app,                          # mount PyWa onto our FastAPI app
    webhook_endpoint="/webhook",                 # -> your callback URL is <public>/webhook
    verify_token=required_env("WA_VERIFY_TOKEN"),  # any string; must match the dashboard
    app_secret=_app_secret,
    validate_updates=bool(_app_secret),          # signature check on if secret is set
)

# --- The brain: GPT-4o Mini via OpenAI API -----------------------------
llm = AsyncOpenAI(api_key=required_env("MODEL_API_KEY"))
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

    sender_id = str(msg.from_user.wa_id)
    incoming_type = "image" if msg.image is not None else "text"
    incoming_body = msg.text or ("Image received" if msg.image is not None else "Unsupported message")
    log_conversation(sender_id, "incoming", incoming_type, incoming_body)

    if msg.image is not None:
        try:
            image_bytes = await msg.image.get_bytes()
            reply = await extract_receipt_text(image_bytes, msg.image.mime_type or "image/jpeg")
        except Exception:
            log.exception("Image OCR failed")
            reply = "Sorry, I couldn't read that image. Please send a different picture."

        try:
            await msg.reply_text(reply)
            log_conversation(sender_id, "outgoing", "text", reply)
        except Exception:
            log.exception("Failed to send image reply to WhatsApp")
        return

    if not msg.text:
        reply = "I can only read text and image messages right now."
        await msg.reply_text(reply)
        log_conversation(sender_id, "outgoing", "text", reply)
        return

    try:
        reply = await generate_text_reply(msg.text)
    except Exception:
        log.exception("LLM call failed")
        reply = "Sorry, I hit a snag. Please try again in a moment."

    try:
        await msg.reply_text(reply)
                log_conversation(sender_id, "outgoing", "text", reply)
    except Exception:
        log.exception("Failed to send reply to WhatsApp")


@fastapi_app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
        return HTMLResponse(DASHBOARD_HTML)


@fastapi_app.get("/dashboard/messages")
async def dashboard_messages():
        return {"messages": list_conversations()}


DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WhatsApp Inbox</title>
<style>
    :root{font-family:Inter,ui-sans-serif,system-ui,sans-serif;color:#17231f;background:#eef4f0}
    *{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 85% 0,#d5e9db,transparent 35%),#eef4f0}
    header{display:flex;justify-content:space-between;align-items:center;padding:26px max(24px,calc((100vw - 1100px)/2));border-bottom:1px solid #d5e1da;background:#f8fbf9cc;backdrop-filter:blur(12px)}
    h1{font-size:clamp(1.5rem,3vw,2.2rem);margin:0;letter-spacing:-.04em}.sub{color:#68776f;margin:6px 0 0}.status{display:flex;gap:8px;align-items:center;color:#52715f;font-size:.9rem}.dot{width:9px;height:9px;border-radius:50%;background:#26a269;box-shadow:0 0 0 4px #d9f1e3}
    main{max-width:1100px;margin:0 auto;padding:28px 24px 50px}.bar{display:flex;justify-content:space-between;align-items:end;margin-bottom:18px}.count{color:#68776f;font-size:.9rem}.refresh{border:1px solid #cad9d0;background:#fff;color:#234235;border-radius:8px;padding:9px 13px;cursor:pointer}.refresh:hover{background:#f1f7f3}
    #messages{display:grid;gap:10px}.empty{padding:64px 20px;text-align:center;color:#7c8d83;background:#fff;border:1px dashed #cbd9d0;border-radius:12px}.message{display:grid;grid-template-columns:120px 1fr;gap:18px;padding:17px 19px;border:1px solid #d7e3db;border-left:4px solid #8aa99a;border-radius:10px;background:#fff;box-shadow:0 4px 16px #28493608}.message.outgoing{border-left-color:#d19b4b;background:#fffdf9}.meta{font-size:.78rem;color:#7b8981;line-height:1.55}.sender{font-weight:700;color:#31493c;word-break:break-word}.direction{font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;font-weight:700;color:#659078}.outgoing .direction{color:#ad7d36}.body{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.55;color:#24362d}.type{font-size:.75rem;color:#89968e;margin-top:4px}@media(max-width:620px){header{padding:22px 18px;align-items:flex-start;gap:15px}.status{font-size:0}.status .dot{font-size:initial}main{padding:20px 14px}.message{grid-template-columns:1fr;gap:7px}.bar{align-items:center}}
</style></head><body>
<header><div><h1>WhatsApp Inbox</h1><p class="sub">Incoming messages and chatbot replies</p></div><div class="status"><span class="dot"></span>Live</div></header>
<main><div class="bar"><div class="count" id="count">Loading messages...</div><button class="refresh" onclick="loadMessages()">Refresh</button></div><section id="messages"><div class="empty">Loading conversation history...</div></section></main>
<script>
const messages = document.getElementById('messages'), count = document.getElementById('count');
function escapeHtml(value){const div=document.createElement('div');div.textContent=value ?? '';return div.innerHTML}
function formatTime(ts){return new Intl.DateTimeFormat([], {dateStyle:'medium',timeStyle:'short'}).format(new Date(ts*1000))}
async function loadMessages(){
    try{const response=await fetch('/dashboard/messages',{cache:'no-store'});const data=await response.json();count.textContent=`${data.messages.length} message${data.messages.length===1?'':'s'} · updates every 5 seconds`;
        messages.innerHTML=data.messages.length?data.messages.map(message=>`<article class="message ${message.direction}"><div class="meta"><div class="direction">${message.direction}</div><div class="sender">${escapeHtml(message.wa_id)}</div><div>${formatTime(message.ts)}</div><div class="type">${escapeHtml(message.message_type)}</div></div><div class="body">${escapeHtml(message.body)}</div></article>`).join(''):'<div class="empty">No messages yet. Send a WhatsApp message to your bot and it will appear here.</div>';
    }catch(error){count.textContent='Dashboard temporarily unavailable';messages.innerHTML='<div class="empty">Could not load messages. Check that the bot server is running.</div>'}
}
loadMessages();setInterval(loadMessages,5000);
</script></body></html>"""


# --- A simple health check you can open in a browser ------------------------
@fastapi_app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL_ID}


# --- Let `python main.py` start the server (same as the uvicorn command) -----
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:fastapi_app", host="0.0.0.0", port=8000, reload=True)