import os
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, HTTPException, Request


app = FastAPI(title="Premium AI Manager")


@dataclass(frozen=True)
class BusinessMessage:
    chat_id: int
    text: str
    business_connection_id: str


def extract_business_message(update: dict) -> BusinessMessage | None:
    message = update.get("business_message")
    if not message or not isinstance(message.get("text"), str):
        return None

    connection_id = message.get("business_connection_id")
    chat_id = message.get("chat", {}).get("id")
    if not connection_id or not isinstance(chat_id, int):
        return None

    return BusinessMessage(chat_id, message["text"].strip(), connection_id)


def allowed_chat_ids() -> set[int]:
    raw = os.getenv("ALLOWED_CHAT_IDS", "")
    return {int(value.strip()) for value in raw.split(",") if value.strip()}


def build_reply(message: BusinessMessage) -> str:
    # ponytail: fixed reply until the approved Russian LLM and prompt are selected.
    return (
        "Спасибо за сообщение. Тестовый AI-менеджер получил ваш запрос: "
        f"«{message.text[:300]}»"
    )


async def send_business_message(message: BusinessMessage, text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "business_connection_id": message.business_connection_id,
        "chat_id": message.chat_id,
        "text": text,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request) -> dict[str, bool]:
    if secret != os.environ.get("TELEGRAM_WEBHOOK_SECRET"):
        raise HTTPException(status_code=404)

    message = extract_business_message(await request.json())
    if message is None or message.chat_id not in allowed_chat_ids():
        return {"ok": True}

    await send_business_message(message, build_reply(message))
    return {"ok": True}
