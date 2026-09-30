import os
import unittest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from app import app, allowed_chat_ids, extract_business_message, lifespan


class BusinessMessageTest(unittest.TestCase):
    def test_extracts_business_message(self) -> None:
        message = extract_business_message(
            {
                "business_message": {
                    "business_connection_id": "connection-1",
                    "chat": {"id": 42, "type": "private"},
                    "from": {"id": 42, "is_bot": False},
                    "text": "  Добрый день  ",
                }
            }
        )
        self.assertIsNotNone(message)
        self.assertEqual(message.chat_id, 42)
        self.assertEqual(message.text, "Добрый день")

    def test_ignores_non_text_updates(self) -> None:
        self.assertIsNone(extract_business_message({"business_message": {}}))

    def test_parses_allowlist(self) -> None:
        with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": "42, 100"}):
            self.assertEqual(allowed_chat_ids(), {42, 100})

    def test_outgoing_messages_are_ignored(self):
        self.assertIsNone(extract_business_message({"business_message": {
            "business_connection_id": "test", "chat": {"id": 42, "type": "private"},
            "from": {"id": 99}, "text": "Outgoing message"}}))

    def test_auth_and_empty_allowlist(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "test-secret", "ALLOWED_CHAT_IDS": ""}), patch("app.send_business_message", new_callable=AsyncMock) as send:
            client = TestClient(app)
            self.assertEqual(client.post("/telegram/webhook", json={}).status_code, 404)
            response = client.post("/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"}, json={"business_message": {
                "business_connection_id": "test", "chat": {"id": 42, "type": "private"},
                "from": {"id": 42}, "text": "Hello"}})
            self.assertEqual(response.status_code, 200)
            send.assert_not_awaited()


class StartupTest(unittest.IsolatedAsyncioTestCase):
    async def test_registers_and_verifies_webhook(self):
        with patch.dict(os.environ, {"RAILWAY_PUBLIC_DOMAIN": "example.test", "PUBLIC_BASE_URL": "", "TELEGRAM_WEBHOOK_SECRET": "test-secret", "ALLOWED_CHAT_IDS": ""}), patch("app.telegram_api", new_callable=AsyncMock) as api:
            api.side_effect = [True, {"url": "https://example.test/telegram/webhook"}]
            async with lifespan(app):
                self.assertTrue(app.state.webhook_ready)
            payload = api.call_args_list[0].args[1]
            self.assertEqual(payload["secret_token"], "test-secret")
            self.assertNotIn("test-secret", payload["url"])
            self.assertNotIn("drop_pending_updates", payload)


if __name__ == "__main__":
    unittest.main()
