import os
import hmac
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, HTTPException, Request


async def telegram_api(method: str, payload: dict):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/{method}", json=payload
            )
            response.raise_for_status()
            data = response.json()
            if data.get("ok") is True:
                return data.get("result")
    except (httpx.HTTPError, ValueError):
        pass
    # Do not expose Telegram URLs, which contain the bot token.
    raise RuntimeError(f"Telegram {method} failed") from None


@asynccontextmanager
async def lifespan(app: FastAPI):
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    base_url = os.getenv("PUBLIC_BASE_URL", "") or (f"https://{domain}" if domain else "")
    app.state.webhook_ready = False
    if base_url:
        secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
        if not base_url.startswith("https://"):
            raise RuntimeError("PUBLIC_BASE_URL must use HTTPS")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret):
            raise RuntimeError("Set TELEGRAM_WEBHOOK_SECRET using letters, digits, _ or -")
        allowed_chat_ids()  # Validate before accepting any messages.
        url = base_url.rstrip("/") + "/telegram/webhook"
        await telegram_api("setWebhook", {
            "url": url,
            "secret_token": secret,
            "allowed_updates": ["business_connection", "business_message"],
        })
        info = await telegram_api("getWebhookInfo", {})
        if info.get("url") != url:
            raise RuntimeError("Telegram webhook verification failed")
        app.state.webhook_ready = True
    yield


app = FastAPI(title="Premium AI Manager", lifespan=lifespan)


@dataclass(frozen=True)
class BusinessMessage:
    chat_id: int
    text: str
    business_connection_id: str


def extract_business_message(update: dict) -> BusinessMessage | None:
    if not isinstance(update, dict):
        return None
    message = update.get("business_message")
    if not isinstance(message, dict) or not isinstance(message.get("text"), str):
        return None

    connection_id = message.get("business_connection_id")
    chat_id = message.get("chat", {}).get("id")
    if not connection_id or not isinstance(chat_id, int):
        return None
    sender = message.get("from", {})
    if (message.get("chat", {}).get("type") != "private"
            or sender.get("id") != chat_id or sender.get("is_bot")
            or message.get("via_business_bot")):
        return None

    return BusinessMessage(chat_id, message["text"].strip(), connection_id)


def allowed_chat_ids() -> set[int] | None:
    raw = os.getenv("ALLOWED_CHAT_IDS", "").strip()
    if raw == "*":
        return None
    return {int(value.strip()) for value in raw.split(",") if value.strip()}


def build_reply(message: BusinessMessage) -> str:
    # ponytail: fixed reply until the approved Russian LLM and prompt are selected.
    return (
        "Спасибо за сообщение. Тестовый AI-менеджер получил ваш запрос: "
        f"«{message.text[:300]}»"
    )


async def send_business_message(message: BusinessMessage, text: str) -> None:
    payload = {
        "business_connection_id": message.business_connection_id,
        "chat_id": message.chat_id,
        "text": text,
    }
    await telegram_api("sendMessage", payload)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "webhook_ready": getattr(app.state, "webhook_ready", False)}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=404)

    message = extract_business_message(await request.json())
    allowed = allowed_chat_ids()
    if message is None or (allowed is not None and message.chat_id not in allowed):
        return {"ok": True}

    await send_business_message(message, build_reply(message))
    return {"ok": True}
