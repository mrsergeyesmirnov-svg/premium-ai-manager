import asyncio
import hmac
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache

import httpx
from fastapi import FastAPI, HTTPException, Request

from sales import (Lead, LeadStore, RANK, ai_enabled, classify, contact_link,
                   declined, demo_fallback, demo_reply, demo_should_summarize, discover_florists,
                   draft_for, lead_display_name, needs_handoff, phone_number, sales_reply, state_for,
                   website_link)


async def telegram_api(method: str, payload: dict, timeout: float = 15):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"https://api.telegram.org/bot{token}/{method}", json=payload)
            response.raise_for_status()
            data = response.json()
            if data.get("ok") is True:
                return data.get("result")
    except (httpx.HTTPError, ValueError):
        pass
    raise RuntimeError(f"Telegram {method} failed") from None


@lru_cache
def get_store() -> LeadStore:
    return LeadStore(os.getenv("SALES_DB_PATH", "/data/sales.db"))


def admin_chat_id() -> int | None:
    value = os.getenv("ADMIN_CHAT_ID", "").strip()
    return int(value) if value and value.lstrip("-").isdigit() else None


async def notify_admin(text: str) -> None:
    chat_id = admin_chat_id()
    if chat_id is None:
        return
    try:
        await telegram_api("sendMessage", {"chat_id": chat_id, "text": text[:4000]})
    except RuntimeError:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_store()
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    base_url = os.getenv("PUBLIC_BASE_URL", "") or (f"https://{domain}" if domain else "")
    app.state.webhook_ready = False
    if base_url:
        secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
        if not base_url.startswith("https://"):
            raise RuntimeError("PUBLIC_BASE_URL must use HTTPS")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret):
            raise RuntimeError("Set TELEGRAM_WEBHOOK_SECRET using letters, digits, _ or -")
        url = base_url.rstrip("/") + "/telegram/webhook"
        await telegram_api("setWebhook", {
            "url": url, "secret_token": secret,
            "allowed_updates": ["business_connection", "business_message", "message"],
        })
        info = await telegram_api("getWebhookInfo", {})
        if info.get("url") != url:
            raise RuntimeError("Telegram webhook verification failed")
        app.state.webhook_ready = True
    yield


app = FastAPI(title="Premium AI Sales Manager", lifespan=lifespan)


@dataclass(frozen=True)
class BusinessMessage:
    chat_id: int
    username: str
    display_name: str
    text: str
    business_connection_id: str
    message_id: int | None


def extract_business_message(update: dict) -> BusinessMessage | None:
    if not isinstance(update, dict):
        return None
    message = update.get("business_message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    if not isinstance(text, str):
        if message.get("photo") or message.get("voice") or message.get("document"):
            text = "[клиент отправил вложение]"
        else:
            return None
    connection_id = message.get("business_connection_id")
    chat = message.get("chat", {})
    sender = message.get("from", {})
    chat_id = chat.get("id")
    if (not connection_id or not isinstance(chat_id, int) or chat.get("type") != "private"
            or sender.get("id") != chat_id or sender.get("is_bot") or message.get("via_business_bot")):
        return None
    display_name = " ".join(filter(None, (sender.get("first_name"), sender.get("last_name"))))
    return BusinessMessage(chat_id, sender.get("username", ""), display_name, text.strip(),
                           connection_id, message.get("message_id"))


def allowed_chat_ids() -> set[int] | None:
    raw = os.getenv("ALLOWED_CHAT_IDS", "*").strip()
    if raw == "*":
        return None
    return {int(value.strip()) for value in raw.split(",") if value.strip()}


async def send_business_message(message: BusinessMessage, text: str) -> None:
    await telegram_api("sendMessage", {
        "business_connection_id": message.business_connection_id,
        "chat_id": message.chat_id,
        "text": text,
    })


def lead_line(lead: Lead) -> str:
    handle = f"@{lead.username}" if lead.username else phone_number(lead) or "без Telegram"
    paused = " · ручной режим" if lead.paused else ""
    mode = " · ДЕМО" if lead.mode == "demo" else ""
    return f"#{lead.id} · {lead.status.upper()}{mode}{paused}\n{lead_display_name(lead)} · {handle} · {lead.city or 'город не указан'}"


def parse_id(text: str) -> int | None:
    match = re.search(r"\s(\d+)\s*$", text)
    return int(match.group(1)) if match else None


HELP = """Продажи цветочным магазинам

/add Название | @username | город | заметка
/find спб — найти лидов через поиск Яндекса
/leads [cold|warm|hot] — последние лиды
/draft ID — подготовить первое сообщение
/sent ID — отметить ручную отправку
/lead ID — карточка и последнее сообщение
/pause ID — отключить автоответы
/resume ID — вернуть автоответы
/demo ID — переключить этот чат в демо онлайн-флориста
/sales ID — вернуть чат в режим обсуждения продукта

Первое холодное сообщение бот не отправляет сам: /draft даёт текст и кнопку перехода в чат."""


async def process_admin_message(update: dict) -> dict[str, bool]:
    message = update.get("message")
    if not isinstance(message, dict):
        return {"ok": True}
    sender_id = message.get("from", {}).get("id")
    text = message.get("text", "").strip()
    if sender_id != admin_chat_id() or message.get("chat", {}).get("type") != "private":
        return {"ok": True}
    store = get_store()
    payload = {"chat_id": sender_id}
    if text in ("/start", "/help"):
        payload["text"] = HELP
    elif text.startswith("/add "):
        parts = [part.strip() for part in text[5:].split("|")]
        if len(parts) < 2 or not parts[0]:
            payload["text"] = "Формат: /add Название | @username | город | заметка"
        else:
            parts += [""] * (4 - len(parts))
            lead = store.add(parts[0], parts[1], parts[2], parts[1], parts[3])
            payload["text"] = "Добавлен:\n" + lead_line(lead) + f"\n\nСледом: /draft {lead.id}"
    elif text.startswith("/find "):
        try:
            found = await discover_florists(text[6:].strip())
            for item in found:
                store.add(**item)
            with_telegram = sum(bool(item["username"]) for item in found)
            payload["text"] = (f"Яндекс: добавлено или обновлено {len(found)} лидов. С Telegram: {with_telegram}.\n"
                               "Выше стоят лиды с прямыми контактами. Это контакты организаций, а не подтверждённые владельцы.\n\n/leads cold")
        except RuntimeError as error:
            payload["text"] = str(error)
        except (ValueError, httpx.HTTPError, KeyError, SyntaxError):
            payload["text"] = ("Поиск Яндекса недоступен. Проверьте роль search-api.webSearch.user, "
                               "платёжный аккаунт и YANDEX_SEARCH_FOLDER_ID.")
    elif text.startswith("/leads"):
        status = text.partition(" ")[2].strip().lower()
        leads = store.list(status)
        payload["text"] = "\n\n".join(map(lead_line, leads)) if leads else "Лидов пока нет."
    elif text.startswith("/draft "):
        lead = store.get(parse_id(text) or 0)
        if not lead:
            payload["text"] = "Лид не найден."
        else:
            payload["text"] = (f"{lead_line(lead)}\n\nТекст первого сообщения:\n\n{draft_for(lead)}"
                               f"\n\nПосле отправки нажмите /sent {lead.id}")
            buttons = []
            telegram = contact_link(lead)
            website = website_link(lead)
            if telegram:
                buttons.append({"text": "Написать в Telegram", "url": telegram})
            if website:
                buttons.append({"text": "Открыть сайт", "url": website})
            if buttons:
                payload["reply_markup"] = {"inline_keyboard": [buttons]}
            else:
                payload["text"] += f"\n\nКонтакт: {lead.contact or 'не найден'}"
    elif text.startswith("/sent "):
        lead_id = parse_id(text) or 0
        lead = store.set_state(lead_id, status="cold") if store.get(lead_id) else None
        payload["text"] = "Отправка отмечена. Теперь ответы этого лида будет обрабатывать менеджер." if lead else "Лид не найден."
    elif text.startswith("/pause ") or text.startswith("/resume "):
        lead_id = parse_id(text) or 0
        lead = store.set_state(lead_id, paused=text.startswith("/pause ")) if store.get(lead_id) else None
        payload["text"] = ("Автоответы остановлены." if lead and lead.paused else "Автоответы включены.") if lead else "Лид не найден."
    elif text == "/demo":
        payload["text"] = ("Укажите номер лида: /demo ID\n\nПосле команды этот же диалог станет демо-магазином. "
                           "Человек сможет написать как покупатель. Вернуть обычный режим: /sales ID")
    elif text.startswith("/demo "):
        lead_id = parse_id(text) or 0
        lead = store.set_mode(lead_id, "demo")
        if lead:
            store.set_setting(f"demo_summary_{lead.id}", "0")
        if not lead:
            payload["text"] = "Лид не найден."
        elif not lead.chat_id or not lead.business_connection_id:
            payload["text"] = ("Демо-режим установлен, но Telegram ещё не связал этот контакт с диалогом. "
                               "Попросите человека сначала ответить вам, затем повторите команду.")
        else:
            intro = ("Спасибо! Я включил демо-режим прямо в этом чате. Теперь можете написать как обычный "
                     "покупатель — например: «Нужен пышный букет маме до 7 000 ₽, без роз».\n\n"
                     "Магазин, ассортимент и цены учебные; настоящий заказ и оплата не создаются.")
            await telegram_api("sendMessage", {"business_connection_id": lead.business_connection_id,
                                                "chat_id": lead.chat_id, "text": intro})
            store.record(lead.id, "out", intro)
            payload["text"] = (f"Демо включено для #{lead.id}. В этот же чат отправлено приглашение.\n"
                               f"Вернуть обсуждение продукта: /sales {lead.id}")
    elif text.startswith("/sales "):
        lead_id = parse_id(text) or 0
        lead = store.set_mode(lead_id, "sales")
        payload["text"] = (f"#{lead.id} возвращён в режим обсуждения продукта." if lead else "Лид не найден.")
    elif text.startswith("/lead "):
        lead = store.get(parse_id(text) or 0)
        payload["text"] = (lead_line(lead) + f"\n\nЗаметка: {lead.note or '—'}\nПоследнее: {lead.last_message or '—'}") if lead else "Лид не найден."
    else:
        payload["text"] = HELP
    await telegram_api("sendMessage", payload)
    return {"ok": True}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "webhook_ready": getattr(app.state, "webhook_ready", False),
            "mode": "sales", "dialogue_mode": "yandex" if ai_enabled() else "scripted"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=404)
    return await process_update(await request.json())


async def process_update(update: dict) -> dict[str, bool]:
    if "message" in update and "business_message" not in update:
        return await process_admin_message(update)
    message = extract_business_message(update)
    allowed = allowed_chat_ids()
    if message is None or (allowed is not None and message.chat_id not in allowed):
        return {"ok": True}
    store = get_store()
    lead = store.match(message.chat_id, message.username, message.business_connection_id)
    if lead is None:
        await notify_admin(
            f"Новый неизвестный контакт: {message.display_name or 'без имени'} "
            f"@{message.username or 'без_username'}\nСообщение: {message.text[:1000]}\n\n"
            "Автоответ не отправлен. Добавьте контакт через /add, если это лид."
        )
        return {"ok": True}
    state = state_for(message.chat_id)
    async with state.lock:
        cached = state.replies.get(message.message_id) if message.message_id is not None else None
        if cached and cached[1]:
            return {"ok": True}
        if not cached and lead.mode == "demo":
            store.record(lead.id, "in", message.text)
            history = store.history(lead.id)
            started = time.monotonic()
            try:
                reply = await demo_reply(history[:-1], message.text)
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                reply = demo_fallback(message.text)
            store.record(lead.id, "out", reply)
            if message.message_id is not None:
                state.replies[message.message_id] = (reply, False)
            await asyncio.sleep(max(0, min(9, 4.5 + len(reply) / 320) - (time.monotonic() - started)))
            summary_key = f"demo_summary_{lead.id}"
            if demo_should_summarize(message.text, history) and store.setting(summary_key) != "1":
                transcript = "\n".join(
                    ("Покупатель: " if item["role"] == "user" else "Флорист: ") + item["text"]
                    for item in history[-8:]
                )
                await notify_admin(f"🧪 ДЕМО: клиент дошёл до выбора\n{lead_line(lead)}\n\n{transcript[:3000]}")
                store.set_setting(summary_key, "1")
        elif not cached:
            store.record(lead.id, "in", message.text)
            incoming_status = classify(message.text)
            status = incoming_status if RANK[incoming_status] > RANK[lead.status] else lead.status
            handoff = needs_handoff(message.text)
            stop = declined(message.text)
            lead = store.set_state(lead.id, status=status, paused=lead.paused or handoff or stop)
            if lead.paused and not (handoff or stop):
                await notify_admin(f"Сообщение в ручном режиме\n{lead_line(lead)}\n\n{message.text[:1500]}")
                return {"ok": True}
            started = time.monotonic()
            reply = await sales_reply(lead, store.history(lead.id)[:-1], message.text)
            store.record(lead.id, "out", reply)
            if message.message_id is not None:
                state.replies[message.message_id] = (reply, False)
            await asyncio.sleep(max(0, min(8, 4 + len(reply) / 350) - (time.monotonic() - started)))
        else:
            reply = cached[0]
        await send_business_message(message, reply)
        if message.message_id is not None:
            state.replies[message.message_id] = (reply, True)
            if len(state.replies) > 50:
                del state.replies[next(iter(state.replies))]
        if lead.mode == "sales" and lead.status in ("warm", "hot"):
            marker = "🔥 ГОРЯЧИЙ" if lead.status == "hot" else "🟡 ТЁПЛЫЙ"
            action = f"\nАвтоответы остановлены. /resume {lead.id}" if lead.paused else ""
            await notify_admin(f"{marker}\n{lead_line(lead)}\n\n{message.text[:1500]}{action}")
    return {"ok": True}
