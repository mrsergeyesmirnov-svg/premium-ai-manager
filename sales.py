"""Small persistent sales queue for a Telegram Business account."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx


log = logging.getLogger("sales")
RANK = {"cold": 0, "warm": 1, "hot": 2}
CITY_BBOXES = {
    "спб": (59.70, 29.40, 60.20, 30.80),
    "санкт-петербург": (59.70, 29.40, 60.20, 30.80),
    "москва": (55.48, 37.29, 55.96, 37.97),
}


def normalize_username(value: str) -> str:
    value = value.strip()
    match = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]{5,})", value, re.I)
    if match:
        value = match.group(1)
    return value.lstrip("@").lower() if re.fullmatch(r"@?[A-Za-z0-9_]{5,}", value) else ""


@dataclass(frozen=True)
class Lead:
    id: int
    name: str
    username: str
    city: str
    contact: str
    note: str
    status: str
    chat_id: int | None
    business_connection_id: str
    paused: bool
    last_message: str


class LeadStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    city TEXT NOT NULL DEFAULT '',
                    contact TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'cold',
                    chat_id INTEGER UNIQUE,
                    business_connection_id TEXT NOT NULL DEFAULT '',
                    paused INTEGER NOT NULL DEFAULT 0,
                    last_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
                    direction TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE UNIQUE INDEX IF NOT EXISTS leads_username_unique
                    ON leads(username) WHERE username <> '';
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _lead(row) -> Lead | None:
        if not row:
            return None
        values = dict(row)
        return Lead(**{name: values[name] for name in Lead.__dataclass_fields__})

    def add(self, name: str, username: str = "", city: str = "", contact: str = "", note: str = "") -> Lead:
        username = normalize_username(username)
        with self.connect() as db:
            if username:
                existing = db.execute("SELECT id FROM leads WHERE username=?", (username,)).fetchone()
                if existing:
                    db.execute("""UPDATE leads SET name=?, city=?, contact=?,
                               note=CASE WHEN ?='' THEN note ELSE ? END, updated_at=CURRENT_TIMESTAMP
                               WHERE id=?""",
                               (name[:200], city[:100], contact[:500], note[:1000], note[:1000], existing["id"]))
                else:
                    db.execute("INSERT INTO leads(name, username, city, contact, note) VALUES (?, ?, ?, ?, ?)",
                               (name[:200], username, city[:100], contact[:500], note[:1000]))
                row = db.execute("SELECT * FROM leads WHERE username=?", (username,)).fetchone()
            else:
                existing = db.execute("SELECT id FROM leads WHERE contact=? AND name=?", (contact[:500], name[:200])).fetchone()
                if existing:
                    db.execute("UPDATE leads SET city=?, note=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                               (city[:100], note[:1000], existing["id"]))
                    row = db.execute("SELECT * FROM leads WHERE id=?", (existing["id"],)).fetchone()
                else:
                    cursor = db.execute(
                        "INSERT INTO leads(name, city, contact, note) VALUES (?, ?, ?, ?)",
                        (name[:200], city[:100], contact[:500], note[:1000]),
                    )
                    row = db.execute("SELECT * FROM leads WHERE id=?", (cursor.lastrowid,)).fetchone()
        return self._lead(row)

    def get(self, lead_id: int) -> Lead | None:
        with self.connect() as db:
            return self._lead(db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())

    def list(self, status: str = "", limit: int = 20) -> list[Lead]:
        with self.connect() as db:
            if status in RANK:
                rows = db.execute(
                    "SELECT * FROM leads WHERE status=? ORDER BY updated_at DESC LIMIT ?", (status, limit)
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM leads ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._lead(row) for row in rows]

    def match(self, chat_id: int, username: str, connection_id: str) -> Lead | None:
        username = normalize_username(username)
        with self.connect() as db:
            row = db.execute("SELECT * FROM leads WHERE chat_id=?", (chat_id,)).fetchone()
            if row is None and username:
                row = db.execute("SELECT * FROM leads WHERE username=?", (username,)).fetchone()
                if row:
                    db.execute(
                        "UPDATE leads SET chat_id=?, business_connection_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (chat_id, connection_id, row["id"]),
                    )
                    row = db.execute("SELECT * FROM leads WHERE id=?", (row["id"],)).fetchone()
            elif row:
                db.execute(
                    "UPDATE leads SET business_connection_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (connection_id, row["id"]),
                )
                row = db.execute("SELECT * FROM leads WHERE id=?", (row["id"],)).fetchone()
        return self._lead(row)

    def record(self, lead_id: int, direction: str, text: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO messages(lead_id, direction, text) VALUES (?, ?, ?)",
                (lead_id, direction, text[:4000]),
            )
            db.execute(
                "UPDATE leads SET last_message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (text[:500], lead_id),
            )

    def history(self, lead_id: int, limit: int = 12) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT direction, text FROM messages WHERE lead_id=? ORDER BY id DESC LIMIT ?",
                (lead_id, limit),
            ).fetchall()[::-1]
        return [{"role": "assistant" if row["direction"] == "out" else "user", "text": row["text"]} for row in rows]

    def set_state(self, lead_id: int, *, status: str | None = None, paused: bool | None = None) -> Lead:
        fields, values = [], []
        if status in RANK:
            fields.append("status=?")
            values.append(status)
        if paused is not None:
            fields.append("paused=?")
            values.append(int(paused))
        if fields:
            values.append(lead_id)
            with self.connect() as db:
                db.execute(f"UPDATE leads SET {', '.join(fields)}, updated_at=CURRENT_TIMESTAMP WHERE id=?", values)
        return self.get(lead_id)


def classify(text: str) -> str:
    text = text.lower().replace("ё", "е")
    if re.search(r"\b(цена|стоим|сколько|созвон|позвон|договор|счет|оплат|готов|подключ|начать|внедр|амо|crm)\w*", text):
        return "hot"
    if re.search(r"\b(интерес|демо|покаж|расскаж|как работает|подробнее|вопрос|можно)\w*", text):
        return "warm"
    return "cold"


def needs_handoff(text: str) -> bool:
    text = text.lower().replace("ё", "е")
    return bool(re.search(r"\b(созвон|позвон|договор|счет|оплат|готов подключ|готов начать|реквизит)\w*", text))


def declined(text: str) -> bool:
    text = text.lower().replace("ё", "е")
    return bool(re.search(r"\b(не интересно|неактуально|не надо|не пишите|откаж)\w*", text))


def draft_for(lead: Lead) -> str:
    detail = f" в {lead.city}" if lead.city else ""
    return (
        f"Здравствуйте! Обратил внимание на «{lead.name}»{detail}. "
        "Я разрабатываю онлайн-менеджера для цветочных магазинов: он отвечает клиентам в Telegram, "
        "подбирает букеты по ассортименту и передаёт заинтересованные заявки сотруднику или в amoCRM. "
        "Сейчас проверяю решение на практике. Можно показать короткое демо? "
        "Если неактуально — больше писать не буду."
    )


def contact_link(lead: Lead) -> str:
    return f"https://t.me/{lead.username}?text={quote(draft_for(lead))}" if lead.username else ""


async def discover_florists(city: str) -> list[dict]:
    bbox = CITY_BBOXES.get(city.lower().strip())
    if not bbox:
        raise ValueError("Доступны: спб, санкт-петербург, москва")
    south, west, north, east = bbox
    query = f'''[out:json][timeout:25];(
      nwr["shop"="florist"]({south},{west},{north},{east});
    );out tags center 100;'''
    async with httpx.AsyncClient(timeout=35, headers={"User-Agent": "premium-ai-manager/1.0"}, trust_env=False) as client:
        response = await client.post("https://overpass-api.de/api/interpreter", content=query)
        response.raise_for_status()
        elements = response.json().get("elements", [])
    found = []
    for item in elements:
        tags = item.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        raw_telegram = tags.get("contact:telegram") or tags.get("telegram") or ""
        website = tags.get("contact:website") or tags.get("website") or ""
        phone = tags.get("contact:phone") or tags.get("phone") or ""
        email = tags.get("contact:email") or tags.get("email") or ""
        username = normalize_username(raw_telegram)
        contact = " · ".join(value for value in (raw_telegram, phone, email, website) if value)
        if contact:
            found.append({"name": name, "username": username, "city": city, "contact": contact,
                          "note": "Открытые данные OpenStreetMap"})
    return found[:100]


def ai_enabled() -> bool:
    return bool(os.getenv("YANDEX_API_KEY") and os.getenv("YANDEX_MODEL_URI"))


async def ai_reply(lead: Lead, history: list[dict], text: str) -> str:
    prompt = f"""Ты деловой онлайн-помощник Сергея Смирнова, разработчика автоматизации для бизнеса.
Собеседник представляет цветочный магазин «{lead.name}». Общайся на русском, спокойно, коротко и профессионально.
Цель: понять, как магазин обрабатывает обращения, показать пользу и договориться о коротком демо или разговоре с Сергеем.
Не выдавай себя за человека. Если спросят прямо, скажи, что ты онлайн-помощник Сергея.
Не выдумывай отзывы, клиентов, кейсы, скидки, функции и договорённости.
Продукт: AI-менеджер в Telegram Business, ответы по ассортименту магазина, квалификация лида,
передача живому сотруднику, сохранение заявок; amoCRM и другие каналы подключаются после изучения текущих интеграций.
Цена: базовая разработка от 40 000 ₽; многоканальность и amoCRM оцениваются после обследования, ориентир до 60 000 ₽.
Поддержка и расходы API рассчитываются отдельно, ориентир поддержки до 5 000 ₽ в месяц.
Не называй цену без прямого вопроса. Не дави и не отправляй повторную рекламу при отказе.
Отвечай сначала по существу, затем задай не больше одного вопроса. Если человек готов к созвону, оплате,
договору или просит точную смету, скажи, что Сергей подключится лично, и не придумывай сроки.
"""
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.post(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            headers={"Authorization": "Api-Key " + os.environ["YANDEX_API_KEY"], "x-data-logging-enabled": "false"},
            json={"modelUri": os.environ["YANDEX_MODEL_URI"],
                  "completionOptions": {"stream": False, "temperature": 0.2, "maxTokens": "500"},
                  "messages": [{"role": "system", "text": prompt}] + history[-12:] + [{"role": "user", "text": text}]},
        )
        if response.status_code >= 400:
            log.warning("Yandex dialogue failed: HTTP %s", response.status_code)
        response.raise_for_status()
        answer = response.json()["result"]["alternatives"][0]["message"]["text"].strip()
        if not answer:
            raise ValueError("empty model response")
        return answer[:3000]


def fallback_reply(text: str) -> str:
    normalized = text.lower().replace("ё", "е")
    if any(word in normalized for word in ("не интересно", "неактуально", "не надо", "откаж")):
        return "Понял, спасибо за ответ. Больше не буду вас отвлекать."
    if any(word in normalized for word in ("сколько", "цена", "стоим")):
        return ("Базовая разработка стоит от 40 000 ₽. Если нужны amoCRM и несколько каналов, "
                "точную стоимость определяем после просмотра текущих интеграций; ориентир — до 60 000 ₽. "
                "Какие каналы обращений вы используете сейчас?")
    if any(word in normalized for word in ("созвон", "позвон", "договор", "счет", "оплат", "реквизит")):
        return "Да, Сергей подключится лично и согласует детали. Напишите, пожалуйста, когда вам удобно связаться."
    if any(word in normalized for word in ("демо", "покаж", "интерес", "подробнее")):
        return ("Да. Помощник отвечает в обычном диалоге Telegram Business, уточняет запрос, работает по вашему "
                "ассортименту и передаёт заинтересованного клиента сотруднику. Где сейчас храните ассортимент и заявки?")
    return ("Спасибо за ответ. Решение настраивается под ассортимент и процессы конкретного магазина, "
            "а сотрудник может подключиться к диалогу в любой момент. Как сейчас обрабатываете обращения из Telegram?")


async def sales_reply(lead: Lead, history: list[dict], text: str) -> str:
    if ai_enabled():
        try:
            return await ai_reply(lead, history, text[:2000])
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            pass
    return fallback_reply(text)


@dataclass
class RuntimeState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    replies: dict = field(default_factory=dict)
    updated: float = field(default_factory=time.monotonic)


states: dict[int, RuntimeState] = {}


def state_for(chat_id: int) -> RuntimeState:
    if chat_id not in states:
        states[chat_id] = RuntimeState()
    states[chat_id].updated = time.monotonic()
    for key, value in list(states.items()):
        if time.monotonic() - value.updated > 86400 and not value.lock.locked():
            del states[key]
    return states[chat_id]
