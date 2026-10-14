import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import app as app_module
from app import app, extract_business_message, lifespan, process_admin_message, process_update
from sales import LeadStore, states


class BusinessMessageTest(unittest.TestCase):
    def test_extracts_inbound_business_message(self):
        message = extract_business_message({"business_message": {
            "message_id": 7, "business_connection_id": "connection-1",
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "username": "FlowerOwner", "first_name": "Анна"},
            "text": " Добрый день ",
        }})
        self.assertEqual((message.chat_id, message.username, message.text), (42, "FlowerOwner", "Добрый день"))

    def test_ignores_outgoing_message(self):
        self.assertIsNone(extract_business_message({"business_message": {
            "business_connection_id": "test", "chat": {"id": 42, "type": "private"},
            "from": {"id": 99}, "text": "Outgoing",
        }}))

    def test_webhook_auth(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "test-secret"}):
            client = TestClient(app)
            self.assertEqual(client.post("/telegram/webhook", json={}).status_code, 404)


class FlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LeadStore(self.tmp.name + "/sales.db")
        app_module.get_store.cache_clear()
        states.clear()

    async def asyncTearDown(self):
        app_module.get_store.cache_clear()
        self.tmp.cleanup()

    async def test_admin_add_and_draft(self):
        with patch.dict(os.environ, {"ADMIN_CHAT_ID": "100"}), patch("app.get_store", return_value=self.store), patch("app.telegram_api", new_callable=AsyncMock) as api:
            await process_admin_message({"message": {"chat": {"id": 100, "type": "private"}, "from": {"id": 100},
                                                     "text": "/add RomRom | @romromshop | СПб | премиум"}})
            lead = self.store.list()[0]
            await process_admin_message({"message": {"chat": {"id": 100, "type": "private"}, "from": {"id": 100},
                                                     "text": f"/draft {lead.id}"}})
            self.assertIn("inline_keyboard", api.call_args.args[1]["reply_markup"])
            self.assertIn("короткое живое демо", api.call_args.args[1]["text"])

    async def test_known_lead_gets_reply_and_hot_alert(self):
        lead = self.store.add("RomRom", "romromshop", "СПб")
        payload = {"business_message": {
            "message_id": 1, "business_connection_id": "bc",
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "username": "romromshop"},
            "text": "Сколько стоит и можно созвониться?",
        }}
        with patch.dict(os.environ, {"ADMIN_CHAT_ID": "100", "ALLOWED_CHAT_IDS": "*", "YANDEX_API_KEY": "", "YANDEX_MODEL_URI": ""}), patch("app.get_store", return_value=self.store), patch("app.send_business_message", new_callable=AsyncMock) as send, patch("app.notify_admin", new_callable=AsyncMock) as notify, patch("app.asyncio.sleep", new_callable=AsyncMock):
            await process_update(payload)
            send.assert_awaited_once()
            notify.assert_awaited_once()
        updated = self.store.get(lead.id)
        self.assertEqual(updated.status, "hot")
        self.assertTrue(updated.paused)

    async def test_unknown_contact_is_not_auto_replied(self):
        payload = {"business_message": {
            "message_id": 1, "business_connection_id": "bc",
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "username": "unknownperson"}, "text": "Привет",
        }}
        with patch.dict(os.environ, {"ADMIN_CHAT_ID": "100", "ALLOWED_CHAT_IDS": "*"}), patch("app.get_store", return_value=self.store), patch("app.send_business_message", new_callable=AsyncMock) as send, patch("app.notify_admin", new_callable=AsyncMock) as notify:
            await process_update(payload)
            send.assert_not_awaited()
            notify.assert_awaited_once()


class StartupTest(unittest.IsolatedAsyncioTestCase):
    async def test_registers_webhook(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "RAILWAY_PUBLIC_DOMAIN": "example.test", "PUBLIC_BASE_URL": "",
            "TELEGRAM_WEBHOOK_SECRET": "test-secret", "SALES_DB_PATH": directory + "/sales.db",
        }), patch("app.telegram_api", new_callable=AsyncMock) as api:
            app_module.get_store.cache_clear()
            api.side_effect = [True, {"url": "https://example.test/telegram/webhook"}]
            async with lifespan(app):
                self.assertTrue(app.state.webhook_ready)
            self.assertIn("message", api.call_args_list[0].args[1]["allowed_updates"])
            app_module.get_store.cache_clear()


if __name__ == "__main__":
    unittest.main()
