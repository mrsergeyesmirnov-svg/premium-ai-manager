import tempfile
import unittest

from sales import LeadStore, classify, contact_link, draft_for, normalize_username


class SalesTest(unittest.TestCase):
    def test_store_matches_reply_and_persists_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LeadStore(directory + "/sales.db")
            lead = store.add("RomRom", "https://t.me/RomRomShop", "СПб")
            matched = store.match(42, "romromshop", "bc")
            self.assertEqual(matched.id, lead.id)
            store.record(lead.id, "in", "Сколько стоит?")
            self.assertEqual(store.history(lead.id)[0]["role"], "user")
            self.assertEqual(store.get(lead.id).chat_id, 42)

    def test_multiple_non_telegram_leads_are_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LeadStore(directory + "/sales.db")
            store.add("Первый", contact="+70000000000")
            store.add("Второй", contact="site.example")
            self.assertEqual(len(store.list()), 2)

    def test_draft_link_and_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            lead = LeadStore(directory + "/sales.db").add("RomRom", "@RomRomShop", "СПб")
            self.assertEqual(normalize_username("https://t.me/RomRomShop"), "romromshop")
            self.assertIn("короткое демо", draft_for(lead))
            self.assertTrue(contact_link(lead).startswith("https://t.me/romromshop?text="))
            self.assertEqual(classify("Можно узнать стоимость и созвониться?"), "hot")
            self.assertEqual(classify("Покажите демо"), "warm")


if __name__ == "__main__":
    unittest.main()
