"""Small persistent sales queue for a Telegram Business account."""
from __future__ import annotations
import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx


log = logging.getLogger("sales")
RANK = {"cold": 0, "warm": 1, "hot": 2}
CITY_SEARCH = {
    "спб": ("Санкт-Петербург", "2"),
    "санкт-петербург": ("Санкт-Петербург", "2"),
    "москва": ("Москва", "213"),
}
SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"
AGGREGATORS = {"yandex.ru", "2gis.ru", "zoon.ru", "yell.ru", "flamp.ru", "vk.com", "t.me"}


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
    mode: str


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
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            try:
                db.execute("ALTER TABLE leads ADD COLUMN mode TEXT NOT NULL DEFAULT 'sales'")
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error).lower():
                    raise

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

    def set_mode(self, lead_id: int, mode: str) -> Lead | None:
        if mode not in ("sales", "demo") or not self.get(lead_id):
            return None
        with self.connect() as db:
            db.execute("UPDATE leads SET mode=?, paused=0, updated_at=CURRENT_TIMESTAMP WHERE id=?", (mode, lead_id))
        return self.get(lead_id)

    def setting(self, key: str, default: str = "") -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def start_demo(self, chat_id: int, username: str, display_name: str, connection_id: str) -> Lead:
        username = normalize_username(username)
        with self.connect() as db:
            row = db.execute("SELECT * FROM leads WHERE chat_id=?", (chat_id,)).fetchone()
            if row is None and username:
                row = db.execute("SELECT * FROM leads WHERE username=?", (username,)).fetchone()
            if row is None:
                cursor = db.execute(
                    "INSERT INTO leads(name, username, contact, note, chat_id, business_connection_id, mode) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'demo')",
                    ((display_name or "Участник демо")[:200], username, f"@{username}" if username else "",
                     "Самостоятельный вход в демо", chat_id, connection_id),
                )
                row = db.execute("SELECT * FROM leads WHERE id=?", (cursor.lastrowid,)).fetchone()
            else:
                db.execute("UPDATE leads SET chat_id=?, business_connection_id=?, mode='demo', paused=0, "
                           "updated_at=CURRENT_TIMESTAMP WHERE id=?", (chat_id, connection_id, row["id"]))
                row = db.execute("SELECT * FROM leads WHERE id=?", (row["id"],)).fetchone()
        return self._lead(row)


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
    return (
        f"Здравствуйте! Меня зовут Сергей. Увидел «{lead_display_name(lead)}» и решил написать лично.\n\n"
        "Я студент из Петербурга, занимаюсь Telegram-ботами и сейчас делаю онлайн-флориста для цветочных "
        "магазинов. Идея появилась из простой ситуации: покупателю бывает трудно объяснить, какой букет он хочет, "
        "а менеджеру приходится снова и снова задавать одни и те же вопросы.\n\n"
        "Помощник может уточнить повод, бюджет и пожелания, подобрать варианты из ассортимента магазина и передать "
        "сотруднику уже понятный запрос. Например, он пригодится, когда человек пишет вечером, просит «пышный букет "
        "маме до 7 000 ₽ без роз» или долго выбирает и уходит без заказа.\n\n"
        "Сейчас я хочу показать идею нескольким магазинам и получить честную обратную связь. Если вам интересно, "
        "я могу прямо в этом чате включить демо-режим: вы напишете как обычный покупатель и сами проверите, "
        "как помощник подбирает букет и ведёт к заказу. Хотите попробовать?\n\n"
        "Если захотите сначала понять, кто я: https://mrsergeyesmirnov-svg.github.io/smirnovsergey/?v=5"
    )


def contact_link(lead: Lead) -> str:
    return f"https://t.me/{lead.username}?text={quote(draft_for(lead))}" if lead.username else ""


def website_link(lead: Lead) -> str:
    for value in re.findall(r"https?://[^\s·]+", lead.contact):
        if "t.me/" not in value.lower():
            return value.rstrip(".,)")
    return ""


def phone_number(lead: Lead) -> str:
    match = re.search(r"(?:\+7|8)[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}", lead.contact)
    return match.group(0).strip() if match else ""


def yandex_folder_id() -> str:
    explicit = os.getenv("YANDEX_SEARCH_FOLDER_ID", "").strip()
    if explicit:
        return explicit
    match = re.match(r"gpt://([^/]+)/", os.getenv("YANDEX_MODEL_URI", ""))
    return match.group(1) if match else ""


def _text(node) -> str:
    return "".join(node.itertext()).strip() if node is not None else ""


GENERIC_TITLE = re.compile(
    r"\b(адрес(?:а|ы)?|магазин(?:ы|ов)?|доставк\w*|цвет(?:ы|ов|очный|очная|очные)?|букет\w*|"
    r"заказ(?:ать)?|купить|каталог|контакт\w*|официальн\w*|сайт|санкт[- ]?петербург|спб|москва)\b",
    re.I,
)


def clean_business_name(title: str, url: str) -> str:
    """Pick the brand-like part instead of a generic SEO page heading."""
    parts = [part.strip(" —–|-:«»\"'") for part in re.split(r"\s*(?:\||—|–| - )\s*", title)]
    candidates = []
    for index, part in enumerate(parts):
        if not part or len(part) > 80:
            continue
        words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", part)
        generic_count = len(GENERIC_TITLE.findall(part))
        score = 8 - len(words) - generic_count * 5 - index * 0.1
        if 1 <= len(words) <= 4 and generic_count == 0:
            score += 10
        candidates.append((score, part))
    if candidates:
        name = max(candidates, key=lambda item: item[0])[1]
        if not GENERIC_TITLE.fullmatch(name):
            return name[:200]
    host = urlparse(url).netloc.lower().removeprefix("www.")
    stem = host.split(".")[0].replace("-", " ").strip()
    return stem.title()[:200] or "Цветочный магазин"


def lead_display_name(lead: Lead) -> str:
    """Also fixes leads saved before title cleanup was introduced."""
    return clean_business_name(lead.name, website_link(lead))


def parse_search_results(raw_xml: bytes, city: str) -> list[dict]:
    root = ET.fromstring(raw_xml)
    leads = []
    for doc in root.findall(".//group/doc"):
        url = _text(doc.find("url"))
        title = re.sub(r"\s+", " ", _text(doc.find("title")))
        snippet = " ".join(_text(node) for node in doc.findall(".//passage"))
        evidence = f"{title} {snippet} {url}"
        if not url or not re.search(r"цвет|букет|флорист", evidence, re.I):
            continue
        username = normalize_username(evidence)
        phone_match = re.search(r"(?:\+7|8)[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}", evidence)
        phone = phone_match.group(0).strip() if phone_match else ""
        host = urlparse(url).netloc.lower().removeprefix("www.")
        own_site = bool(host and not any(host == item or host.endswith("." + item) for item in AGGREGATORS))
        score = 6 * bool(username) + 4 * own_site + 3 * bool(phone) + 1
        name = clean_business_name(title, url)
        contact = " · ".join(dict.fromkeys(value for value in (f"@{username}" if username else "", phone, url) if value))
        leads.append({"name": name, "username": username, "city": city, "contact": contact,
                      "note": f"Яндекс · качество {score}/14 · {snippet[:500]}", "score": score})
    return leads


async def _yandex_search(query: str, city: str, region: str, api_key: str, folder_id: str) -> list[dict]:
    payload = {
        "query": {"searchType": "SEARCH_TYPE_RU", "queryText": query, "familyMode": "FAMILY_MODE_MODERATE"},
        "groupSpec": {"groupMode": "GROUP_MODE_FLAT", "groupsOnPage": "30", "docsInGroup": "1"},
        "maxPassages": "2", "region": region, "l10n": "LOCALIZATION_RU",
        "folderId": folder_id, "responseFormat": "FORMAT_XML",
    }
    async with httpx.AsyncClient(timeout=35, trust_env=False) as client:
        response = await client.post(SEARCH_URL, headers={"Authorization": "Api-Key " + api_key,
                                                          "x-data-logging-enabled": "false"}, json=payload)
        response.raise_for_status()
        raw = base64.b64decode(response.json()["rawData"], validate=True)
    return parse_search_results(raw, city)


async def discover_florists(city: str) -> list[dict]:
    place = CITY_SEARCH.get(city.lower().strip())
    if not place:
        raise ValueError("Доступны: спб, санкт-петербург, москва")
    api_key, folder_id = os.getenv("YANDEX_API_KEY", "").strip(), yandex_folder_id()
    if not api_key or not folder_id:
        raise RuntimeError("Для поиска нужны YANDEX_API_KEY и YANDEX_SEARCH_FOLDER_ID")
    city_name, region = place
    queries = (
        f'"цветочный магазин" {city_name} контакты',
        f'"цветочная мастерская" {city_name} заказать букет',
        f'site:t.me цветы магазин {city_name}',
    )
    batches = await asyncio.gather(*(_yandex_search(query, city_name, region, api_key, folder_id) for query in queries))
    unique = {}
    for lead in (item for batch in batches for item in batch):
        lead["city"] = city_name
        key = lead["username"] or urlparse(lead["contact"].split(" · ")[-1]).netloc + lead["name"].lower()
        if key and (key not in unique or lead["score"] > unique[key]["score"]):
            unique[key] = lead
    result = sorted(unique.values(), key=lambda item: (-item["score"], item["name"].lower()))[:60]
    for lead in result:
        lead.pop("score")
    return result


def ai_enabled() -> bool:
    return bool(os.getenv("YANDEX_API_KEY") and os.getenv("YANDEX_MODEL_URI"))


async def ai_reply(lead: Lead, history: list[dict], text: str) -> str:
    prompt = f"""Ты доброжелательный онлайн-помощник Сергея Смирнова. Сергей — студент из Петербурга,
занимается Telegram-ботами и развивает онлайн-флориста для цветочных магазинов.
Собеседник представляет цветочный магазин «{lead_display_name(lead)}».

Пиши по-русски, тепло и естественно, как внимательный собеседник, а не бездушный продажник.
Используй простые слова, обычно 2–5 предложений. Сначала пойми и признай мысль человека, затем ответь по существу.
Не используй канцелярит, маркетинговые штампы, чрезмерную похвалу и давление. Не повторяй предложение демо в каждом ответе.
Задавай не больше одного уместного вопроса за раз и не превращай разговор в анкету.

Цель: спокойно разобраться, может ли решение быть полезно магазину, и только при взаимном интересе предложить
короткое демо или разговор с Сергеем. Сайт Сергея: https://mrsergeyesmirnov-svg.github.io/smirnovsergey/?v=5
Не выдавай себя за человека. Если спросят прямо, скажи, что ты онлайн-помощник Сергея.
Не выдумывай отзывы, клиентов, кейсы, скидки, функции и договорённости.
Продукт: AI-менеджер в Telegram Business, ответы по ассортименту магазина, квалификация лида,
передача живому сотруднику, сохранение заявок; amoCRM и другие каналы подключаются после изучения текущих интеграций.
Если человек хочет демо, объясни: Сергей может переключить этот же чат в режим учебного цветочного магазина,
после чего человек сможет написать как обычный покупатель. Не предлагай отдельную ссылку или другого бота.
Примеры пользы: ответить вечером или во время загрузки менеджера; понять свободный запрос вроде
«пышный букет маме до 7 000 ₽ без роз»; подобрать варианты из актуального ассортимента; уточнить доставку;
вернуть клиента, который выбирал, но не закончил заказ; вовремя передать сложный или дорогой запрос человеку.
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
        return "Конечно. Я передам Сергею, и он подключится лично. Когда вам было бы удобно спокойно всё обсудить?"
    if any(word in normalized for word in ("кто вы", "кто такой", "кто сергей", "ваш сайт", "портфолио", "о себе")):
        return ("Я онлайн-помощник Сергея Смирнова. Сам Сергей — студент из Петербурга, занимается Telegram-ботами "
                "и сейчас развивает онлайн-флориста. Вот его личный сайт: "
                "https://mrsergeyesmirnov-svg.github.io/smirnovsergey/?v=5")
    if any(word in normalized for word in ("демо", "покаж", "интерес", "подробнее")):
        return ("Конечно. Сергей может переключить прямо этот чат в демо-режим цветочного магазина. После этого "
                "вы сможете написать как обычный покупатель — например, попросить пышный букет маме до 7 000 ₽ "
                "без роз — и самостоятельно пройти весь диалог. Включить демо?")
    return ("Спасибо, что ответили. Я постараюсь помочь по существу, без навязчивых предложений. "
            "Расскажите, пожалуйста, что в идее вызывает у вас вопрос или сомнение?")


async def sales_reply(lead: Lead, history: list[dict], text: str) -> str:
    if ai_enabled():
        try:
            return await ai_reply(lead, history, text[:2000])
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            pass
    return fallback_reply(text)


DEMO_CATALOG = """Учебный магазин «Тихий сад», Санкт-Петербург. Ежедневно 09:00–22:00.
Самовывоз бесплатный, доставка по городу 700 ₽.

Актуальный учебный ассортимент:
1. «Белый шёлк» — 4 900 ₽: 7 эустом и эвкалипт. Белый, воздушный, сдержанный.
2. «Тёплое утро» — 5 900 ₽: 5 кустовых хризантем, 4 эустомы и эвкалипт. Кремовый и мягкий.
3. «Лавандовый вечер» — 6 900 ₽: 3 гортензии и 5 эустом. Сиреневый, объёмный, нежный.
4. «Розовый акцент» — 7 900 ₽: 11 роз и эвкалипт. Классический и романтичный.
5. «Графика» — 8 900 ₽: антуриумы и декоративная зелень. Современный и выразительный.
6. «Большое чувство» — 12 900 ₽: 5 гортензий, 7 эустом и эвкалипт. Пышный и торжественный.

Открытка и упаковка включены. Дополнительно доступны ваза и конфеты. Их цену называй только после того,
как покупатель заинтересовался конкретным дополнением: ваза — 1 500 ₽, конфеты — 900 ₽.
"""


async def demo_reply(history: list[dict], text: str) -> str:
    prompt = f"""Ты онлайн-флорист учебного магазина. Это демонстрация для владельца цветочного бизнеса.
{DEMO_CATALOG}

Общайся по-русски, тепло, уверенно и естественно, без канцелярита и навязчивости. Обычно пиши 2–5 предложений.
Сначала пойми запрос, затем задай только один действительно нужный вопрос. Учитывай повод, получателя, бюджет,
нежелательные цветы, дату и способ получения. Не повторяй уже полученные сведения.
Предлагай максимум три подходящих варианта только из каталога и кратко объясняй разницу между ними.
Не придумывай наличие, цены, фотографии и составы. Если просят изменить состав или количество цветов,
скажи, что это потребует подтверждения флориста, и предложи ближайший вариант из каталога.
Допродажу предлагай мягко только после выбора букета. Сначала назови дополнения без цен; цену сообщай,
только если человек заинтересовался. Не предлагай несколько дополнений повторно.
Когда вариант выбран, уточни дату/время и доставку или самовывоз. Не проси настоящие персональные данные:
предложи использовать вымышленные имя, телефон и адрес. Деньги не принимай, ссылку на оплату не давай.
В финале собери короткое «Учебное резюме заказа» и обязательно скажи, что заказ и оплата не создавались.
Если спросят, честно скажи, что это демо онлайн-флориста Сергея, работающее через Яндекс API.
"""
    if not ai_enabled():
        return demo_fallback(text)
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.post(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            headers={"Authorization": "Api-Key " + os.environ["YANDEX_API_KEY"],
                     "x-data-logging-enabled": "false"},
            json={"modelUri": os.environ["YANDEX_MODEL_URI"],
                  "completionOptions": {"stream": False, "temperature": 0.35, "maxTokens": "650"},
                  "messages": [{"role": "system", "text": prompt}] + history[-16:] +
                              [{"role": "user", "text": text[:2000]}]},
        )
        response.raise_for_status()
        answer = response.json()["result"]["alternatives"][0]["message"]["text"].strip()
        if not answer:
            raise ValueError("empty model response")
        return answer[:3500]


def demo_fallback(text: str) -> str:
    normalized = text.lower().replace("ё", "е")
    if any(word in normalized for word in ("заказ", "оформ", "беру", "подходит")):
        return ("Отлично, зафиксируем выбранный букет. Подскажите, пожалуйста, это будет доставка или самовывоз? "
                "Для демо используйте только вымышленные контактные данные — настоящий заказ не создаётся.")
    if any(word in normalized for word in ("мам", "пыш", "7000", "7 000", "без роз")):
        return ("Для такого запроса я бы предложил «Лавандовый вечер» за 6 900 ₽: три гортензии и пять эустом, "
                "букет получается объёмным и нежным. Роз в составе нет. Как вам сиреневая гамма?")
    return ("Здравствуйте! Это демо онлайн-флориста магазина «Тихий сад». Расскажите, пожалуйста, для кого "
            "вы выбираете букет и на какой бюджет ориентируетесь?")


def demo_should_summarize(text: str, history: list[dict]) -> bool:
    normalized = text.lower().replace("ё", "е")
    return bool(re.search(r"\b(оформ|заказ|беру|подходит|достав|самовывоз|адрес|оплат)\w*", normalized)) or len(history) >= 10


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
