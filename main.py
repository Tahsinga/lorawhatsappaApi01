"""
WhatsApp bot: receives client messages, replies using Muse Spark (Meta Model API).

Stack:
  - PyWa (async)      -> handles the WhatsApp Cloud API webhook + sending
  - FastAPI + uvicorn -> the web server that Meta calls
  - OpenAI client      -> points at Meta Model API (it's OpenAI-compatible)

Run:
  uvicorn main:fastapi_app --host 0.0.0.0 --port 8000 --reload
"""

import asyncio
import base64
import logging
import mimetypes
import os
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from openai import AsyncOpenAI
from pywa_async import WhatsApp, types
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from store import (
    BOT_SESSION_TIMEOUT_SECONDS,
    clear_bot_session,
    get_bot_session,
    init_db,
    is_bot_session_expired,
    list_conversations,
    log_conversation,
    set_bot_session,
    touch_bot_session,
)

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

MEDIA_DIR = Path(__file__).with_name("media")


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
STOCK_API_URL = os.getenv(
    "LORA_STOCK_API_URL",
    "https://laxdomloraapi.onrender.com/api/whatsapp/stock/",
).rstrip("/") + "/"
STOCK_API_TOKEN = os.getenv("WHATSAPP_STOCK_API_TOKEN", "")
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


async def fetch_stock_data(params: dict[str, str]) -> dict[str, Any]:
    if not STOCK_API_TOKEN:
        raise RuntimeError("WHATSAPP_STOCK_API_TOKEN is not configured")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            STOCK_API_URL,
            params=params,
            headers={"X-WhatsApp-Token": STOCK_API_TOKEN},
        )
        response.raise_for_status()
        payload = response.json()
    if payload.get("status") != "ok":
        raise RuntimeError("Stock API returned an unsuccessful response")
    return payload


async def branch_menu(greeting: bool = False) -> str:
    payload = await fetch_stock_data({"action": "branches"})
    branches = payload.get("branches", [])
    heading = "👋 *WELCOME TO LORA RETURNS CONNECT*\n\n" if greeting else ""
    if not branches:
        return (
            f"{heading}📦 *STOCK SENT TODAY*\n"
            "🏬 *SELECT YOUR BRANCH*\n\n"
            "No branches are available right now. Please try again later or type MENU to restart."
        )
    lines = [
        f"{heading}📦 *STOCK SENT TODAY*\n"
        "🏬 *SELECT YOUR BRANCH*\n\n"
        "Choose a branch to see what was sent today:\n\n",
    ]
    lines.extend(f"{index}️⃣ *{name}*\n" for index, name in enumerate(branches, start=1))
    lines.append("\nReply with the branch number to continue.\nType MENU anytime to start again.")
    return "".join(lines)


def transfer_reply(
    branch: str,
    transfer_date: str,
    transfers: list[dict[str, Any]],
    is_today: bool = True,
) -> list[str]:
    try:
        display_date = datetime.fromisoformat(transfer_date).strftime("%A, %d %B %Y")
    except ValueError:
        display_date = transfer_date
    report_title = "STOCK SENT TODAY" if is_today else "STOCK TRANSFERS FOR SELECTED DATE"
    if not transfers:
        return [
            f"📦 *{report_title}*\n🏬 *{branch}*\n📅 {display_date}\n\n"
            "No stock transfers were found for this branch and date.\n\n"
            "Reply 1 for today's transfers, 2 to choose a date, 3 to change branch, or MENU to start again."
        ]

    total_quantity = sum(int(Decimal(str(item.get("quantity", 0)))) for item in transfers)
    heading = (
        f"📦 *{report_title}*\n🏬 *{branch}*\n📅 {display_date}\n"
        f"📊 {len(transfers)} deliveries · {total_quantity} units total\n\n"
    )
    entries = []
    for transfer in transfers:
        product = transfer.get("product_name") or f"Product {transfer.get('product_id', '')}"
        timestamp = str(transfer.get("sent_at", ""))
        try:
            timestamp = datetime.fromisoformat(timestamp).strftime("%I:%M %p").lstrip("0")
        except ValueError:
            timestamp = "Time unavailable"
        status = str(transfer.get("status", "sent")).replace("_", " ").title()
        entries.append(
            f"📦 *{product}*\n"
            f"   Quantity: *{transfer.get('quantity', '?')}*\n"
            f"   Sent: *{timestamp}* · {status}\n"
        )

    pages = []
    current = heading
    continued_heading = f"📦 *{report_title} · CONTINUED*\n🏬 *{branch}*\n📅 {display_date}\n\n"
    for entry in entries:
        if len(current) + len(entry) > 3300:
            pages.append(current.rstrip())
            current = continued_heading
        current += entry + "\n"
    current += "Reply 1 for today's transfers, 2 to choose a date, 3 to change branch, or MENU to start again."
    pages.append(current.rstrip())
    return pages


async def stock_menu_reply(sender_id: str, user_text: str) -> str:
    normalized = user_text.strip().casefold()
    session = get_bot_session(sender_id)

    if normalized in {"menu", "start"} or not session:
        set_bot_session(sender_id, "choose_branch")
        try:
            return await branch_menu(greeting=True)
        except Exception:
            log.exception("Failed to load branches from live stock API")
            return "Hi! Welcome to Lora Returns Connect. I can't reach the live stock system right now. Please try again shortly."

    if session["stage"] == "choose_branch":
        try:
            branches = (await fetch_stock_data({"action": "branches"})).get("branches", [])
        except Exception:
            log.exception("Failed to load branches from live stock API")
            return "I can't reach the live stock system right now. Please try again shortly."
        selected = None
        if normalized.isdigit():
            index = int(normalized) - 1
            if 0 <= index < len(branches):
                selected = branches[index]
        else:
            selected = next((name for name in branches if name.casefold() == normalized), None)
        if selected is None:
            return "Please reply with a listed branch number or its exact name.\n\n" + "\n".join(
                f"{index}. {name}" for index, name in enumerate(branches, start=1)
            )
        set_bot_session(sender_id, "choose_action", selected)
        return (
            f"📦 *STOCK SENT TODAY*\n🏬 *{selected}*\n\n"
            "Choose an option:\n\n"
            "1️⃣ *View today's stock transfers*\n"
            "2️⃣ *Choose a date*\n"
            "3️⃣ *Choose another branch*\n\n"
            "Reply with 1, 2, or 3, or type MENU to start again."
        )

    branch = session.get("branch", "")
    if session["stage"] == "choose_date":
        if normalized in {"3", "branch", "change branch"}:
            set_bot_session(sender_id, "choose_branch")
            try:
                return await branch_menu()
            except Exception:
                log.exception("Failed to load branches from live stock API")
                return "I can't reach the live stock system right now. Please try again shortly."
        try:
            selected_date = datetime.strptime(user_text.strip(), "%Y-%m-%d").date()
        except ValueError:
            return "Please send the date as YYYY-MM-DD, for example 2026-10-19. Or reply 3 to change branch."
        try:
            payload = await fetch_stock_data({"branch": branch, "date": selected_date.isoformat()})
            return transfer_reply(
                branch,
                payload.get("date", selected_date.isoformat()),
                payload.get("transfers", []),
                is_today=False,
            )
        except Exception:
            log.exception("Failed to load dated branch transfers from live stock API")
            return "I can't load transfers for that date right now. Please try again shortly."

    if normalized in {"3", "branch", "change branch"}:
        set_bot_session(sender_id, "choose_branch")
        try:
            return await branch_menu()
        except Exception:
            log.exception("Failed to load branches from live stock API")
            return "I can't reach the live stock system right now. Please try again shortly."
    if normalized == "2":
        set_bot_session(sender_id, "choose_date", branch)
        return (
            f"📅 *CHOOSE A DATE*\n🏬 *{branch}*\n\n"
            "Send the date you want to check in this format:\n"
            "YYYY-MM-DD\n\n"
            "Example: 2026-10-19\n"
            "Reply 3 to choose another branch, or MENU to start again."
        )
    if normalized in {"1", "transfers", "transfer", "stock", "view", "see"}:
        try:
            payload = await fetch_stock_data({"branch": branch})
            return transfer_reply(branch, payload.get("date", "Today"), payload.get("transfers", []))
        except Exception:
            log.exception("Failed to load branch transfers from live stock API")
            return "I can't load transfers from the live stock system right now. Please try again shortly."
    return (
        "Reply 1 for today's transfers, 2 to choose a date, 3 to change branch, "
        "or MENU to start again."
    )


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


SESSION_TIMEOUT_REPLY = (
    "⏳ Your session has timed out after 5 minutes of inactivity. "
    "Send any message to start again."
)
_session_timeout_tasks: dict[str, asyncio.Task[None]] = {}


async def _notify_session_timeout(sender_id: str, updated_at: float) -> None:
    try:
        await asyncio.sleep(BOT_SESSION_TIMEOUT_SECONDS)
        session = get_bot_session(sender_id)
        if not session or session["updated_at"] != updated_at:
            return

        clear_bot_session(sender_id)
        await wa.send_message(to=sender_id, text=SESSION_TIMEOUT_REPLY)
        log_conversation(sender_id, "outgoing", "text", SESSION_TIMEOUT_REPLY)
    except Exception:
        log.exception("Failed to send WhatsApp session timeout notification")
    finally:
        if _session_timeout_tasks.get(sender_id) is asyncio.current_task():
            _session_timeout_tasks.pop(sender_id, None)


def _schedule_session_timeout(sender_id: str, updated_at: float) -> None:
    previous_task = _session_timeout_tasks.get(sender_id)
    if previous_task:
        previous_task.cancel()
    _session_timeout_tasks[sender_id] = asyncio.create_task(
        _notify_session_timeout(sender_id, updated_at)
    )


# --- Handle every incoming text, image, and audio message ------------------
@wa.on_message()  # type: ignore[arg-type]  # pywa async-stub quirk; verified correct at runtime
async def handle_message(_: WhatsApp, msg: types.Message):
    log.debug("Entered handle_message handler")
    log.info("Message from %s: %s", msg.from_user.wa_id, msg.text)

    sender_id = str(msg.from_user.wa_id)
    incoming_type = "image" if msg.image is not None else ("audio" if msg.audio is not None else "text")
    incoming_body = msg.text or (
        "Image received" if msg.image is not None else
        "Audio received" if msg.audio is not None else
        "Unsupported message"
    )

    session = get_bot_session(sender_id)
    if session and is_bot_session_expired(session["updated_at"]):
        log_conversation(sender_id, "incoming", incoming_type, incoming_body)
        clear_bot_session(sender_id)
        try:
            await msg.reply_text(SESSION_TIMEOUT_REPLY)
            log_conversation(sender_id, "outgoing", "text", SESSION_TIMEOUT_REPLY)
        except Exception:
            log.exception("Failed to send timed-out session reply to WhatsApp")
        return
    if session:
        touch_bot_session(sender_id)
        refreshed_session = get_bot_session(sender_id)
        if refreshed_session:
            _schedule_session_timeout(sender_id, refreshed_session["updated_at"])

    if msg.image is not None:
        image_logged = False
        try:
            image_bytes = await msg.image.get_bytes()
            mime_type = msg.image.mime_type or "image/jpeg"
            extension = mimetypes.guess_extension(mime_type) or ".jpg"
            filename = f"{uuid.uuid4().hex}{extension}"
            MEDIA_DIR.mkdir(exist_ok=True)
            (MEDIA_DIR / filename).write_bytes(image_bytes)
            log_conversation(sender_id, "incoming", incoming_type, filename)
            image_logged = True
            reply = await extract_receipt_text(image_bytes, mime_type)
        except Exception:
            log.exception("Image OCR failed")
            if not image_logged:
                log_conversation(sender_id, "incoming", incoming_type, incoming_body)
            reply = "Sorry, I couldn't read that image. Please send a different picture."

        try:
            await msg.reply_text(reply)
            log_conversation(sender_id, "outgoing", "text", reply)
        except Exception:
            log.exception("Failed to send image reply to WhatsApp")
        return

    if msg.audio is not None:
        audio_logged = False
        try:
            audio_bytes = await msg.audio.get_bytes()
            mime_type = msg.audio.mime_type or "audio/ogg"
            extension = mimetypes.guess_extension(mime_type) or ".ogg"
            filename = f"{uuid.uuid4().hex}{extension}"
            MEDIA_DIR.mkdir(exist_ok=True)
            (MEDIA_DIR / filename).write_bytes(audio_bytes)
            log_conversation(sender_id, "incoming", "audio", filename)
            audio_logged = True
        except Exception:
            log.exception("Audio download failed")
            if not audio_logged:
                log_conversation(sender_id, "incoming", "audio", incoming_body)

        reply = "I received your audio message."
        try:
            await msg.reply_text(reply)
            log_conversation(sender_id, "outgoing", "text", reply)
        except Exception:
            log.exception("Failed to send audio reply to WhatsApp")
        return

    log_conversation(sender_id, "incoming", incoming_type, incoming_body)

    if not msg.text:
        reply = "I can only read text and image messages right now."
        await msg.reply_text(reply)
        log_conversation(sender_id, "outgoing", "text", reply)
        return

    try:
        reply = await stock_menu_reply(sender_id, msg.text)
    except Exception:
        log.exception("WhatsApp stock menu failed")
        reply = "Sorry, I hit a snag. Please try again in a moment."

    session = get_bot_session(sender_id)
    if session:
        _schedule_session_timeout(sender_id, session["updated_at"])

    try:
        replies = reply if isinstance(reply, list) else [reply]
        for outgoing_reply in replies:
            await msg.reply_text(outgoing_reply)
            log_conversation(sender_id, "outgoing", "text", outgoing_reply)
    except Exception:
        log.exception("Failed to send reply to WhatsApp")


@fastapi_app.get("/", include_in_schema=False)
async def home():
    return RedirectResponse(url="/dashboard")


@fastapi_app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


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