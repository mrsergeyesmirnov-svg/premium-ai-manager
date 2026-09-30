import os
import unittest

from app import allowed_chat_ids, extract_business_message


class BusinessMessageTest(unittest.TestCase):
    def test_extracts_business_message(self) -> None:
        message = extract_business_message(
            {
                "business_message": {
                    "business_connection_id": "connection-1",
                    "chat": {"id": 42},
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
        os.environ["ALLOWED_CHAT_IDS"] = "42, 100"
        self.assertEqual(allowed_chat_ids(), {42, 100})


if __name__ == "__main__":
    unittest.main()
