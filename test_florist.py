import os
import unittest
from unittest.mock import patch, AsyncMock

from florist import Session, respond, available, get_session, sessions
from app import app
from fastapi.testclient import TestClient


class FloristTest(unittest.IsolatedAsyncioTestCase):
    async def test_lush_custom_request_and_confirmed_extras(self):
        with patch.dict(os.environ, {"YANDEX_API_KEY": ""}):
            s = Session()
            await respond(s, "Пышный букет маме до 15к")
            self.assertEqual(s.choices[0]["name"], "Большое чувство")
            await respond(s, "второй")
            answer = await respond(s, "доставка")
            self.assertIn("Итого: 7600 ₽", answer)
            self.assertIn("Хотели бы", answer)
            self.assertNotIn("вазу за", answer.lower())
            self.assertNotIn("конфеты за", answer.lower())
            answer = await respond(s, "Да, интересно")
            self.assertIn("Ваза — 1 500 ₽", answer)
            self.assertIn("конфеты — 900 ₽", answer)
            answer = await respond(s, "Можно добавить еще 3 гортензии?")
            self.assertIn("согласовать с флористом", answer)
            self.assertNotIn("Предварительный расчёт", answer)
            await respond(s, "Сколько стоит ваза?")
            self.assertFalse(s.extras)
            answer = await respond(s, "Добавьте вазу")
            self.assertIn("Итого: 9100 ₽", answer)
            answer = await respond(s, "Добавьте вазу")
            self.assertIn("Итого: 9100 ₽", answer)
            answer = await respond(s, "Уберите вазу")
            self.assertIn("Итого: 7600 ₽", answer)
            self.assertNotIn("Хотели бы", answer)

    async def test_full_demo_no_roses_budget_selection_and_reset(self):
        with patch.dict(os.environ, {"YANDEX_API_KEY": "", "YANDEX_MODEL_URI": ""}):
            s = Session()
            answer = await respond(s, "Маме до 7 тысяч, без роз")
            self.assertIn("демо", answer)
            self.assertIn("Лавандовый вечер", answer)
            self.assertTrue(all(b["price"] <= 7000 and "роз" not in b["tags"] for b in s.choices))
            await respond(s, "второй")
            answer = await respond(s, "самовывоз")
            self.assertIn("Итого: 5900 ₽", answer)
            self.assertIn("заказ не создан", answer)
            await respond(s, "заново")
            self.assertIsNone(s.budget)
            self.assertEqual(s.excluded, set())

    async def test_exclusions_persist_and_unavailable_not_offered(self):
        with patch.dict(os.environ, {"YANDEX_API_KEY": "", "YANDEX_MODEL_URI": ""}):
            s = Session()
            await respond(s, "Она не любит розы")
            await respond(s, "до 15000")
            self.assertTrue(all("роз" not in b["tags"] and b["stock"] > 0 for b in available(s)))
            await respond(s, "еще варианты")
            self.assertTrue(all("роз" not in b["tags"] for b in s.choices))
            self.assertIn("нет", await respond(s, "Есть пионы?"))

    async def test_ai_failure_and_separate_sessions(self):
        self.assertIsNot(get_session("one", 1), get_session("two", 1))
        with patch("florist.ai_enabled", return_value=True), patch("florist.ai_reply", new_callable=AsyncMock, side_effect=ValueError("secret")):
            answer = await respond(Session(), "Привет")
            self.assertNotIn("secret", answer)
            self.assertIn("Не удалось", answer)


class WebhookTest(unittest.TestCase):
    def test_wildcard_accepts_inbound_and_deduplicates(self):
        sessions.clear()
        with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": "*", "TELEGRAM_WEBHOOK_SECRET": "test", "YANDEX_API_KEY": ""}), patch("app.send_business_message", new_callable=AsyncMock) as send:
            client = TestClient(app)
            payload = {"business_message": {"message_id": 1, "business_connection_id": "test",
                "chat": {"id": 123, "type": "private"}, "from": {"id": 123}, "text": "Привет"}}
            for _ in range(2):
                result = client.post("/telegram/webhook", json=payload, headers={"X-Telegram-Bot-Api-Secret-Token": "test"})
                self.assertEqual(result.status_code, 200)
            send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
