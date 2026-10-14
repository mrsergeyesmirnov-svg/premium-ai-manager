import tempfile
import unittest

from sales import (LeadStore, classify, clean_business_name, contact_link, draft_for, lead_display_name,
                   normalize_username, parse_search_results, website_link, yandex_folder_id)


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
            self.assertIn("короткое живое демо", draft_for(lead))
            self.assertIn("Меня зовут Сергей", draft_for(lead))
            self.assertIn("mrsergeyesmirnov-svg.github.io", draft_for(lead))
            self.assertNotIn("amoCRM", draft_for(lead))
            self.assertTrue(contact_link(lead).startswith("https://t.me/romromshop?text="))
            self.assertEqual(classify("Можно узнать стоимость и созвониться?"), "hot")
            self.assertEqual(classify("Покажите демо"), "warm")

    def test_parses_and_scores_yandex_results(self):
        raw = '''<?xml version="1.0" encoding="utf-8"?>
        <yandexsearch><response><results><grouping><group><doc>
        <url>https://t.me/romromflowers</url><title>RomRom — цветочный магазин</title>
        <passages><passage>Букеты в Санкт-Петербурге +7 (999) 123-45-67</passage></passages>
        </doc></group><group><doc><url>https://example.com/cars</url><title>Автомобили</title></doc></group>
        </grouping></results></response></yandexsearch>'''.encode()
        leads = parse_search_results(raw, "Санкт-Петербург")
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["username"], "romromflowers")
        self.assertIn("+7 (999) 123-45-67", leads[0]["contact"])

    def test_cleans_seo_title_and_extracts_website(self):
        self.assertEqual(clean_business_name(
            "Адреса магазинов - Весна - Доставка цветов в Санкт-Петербурге",
            "https://vesnabuket.ru/adresa-magazinov/",
        ), "Весна")
        with tempfile.TemporaryDirectory() as directory:
            lead = LeadStore(directory + "/sales.db").add(
                "Адреса магазинов - Весна - Доставка цветов в Санкт-Петербурге",
                contact="+7 (904) 215-91-35 · https://vesnabuket.ru/adresa-magazinov/"
            )
            self.assertEqual(website_link(lead), "https://vesnabuket.ru/adresa-magazinov/")
            self.assertEqual(lead_display_name(lead), "Весна")

    def test_infers_search_folder_from_model_uri(self):
        from unittest.mock import patch
        with patch.dict("os.environ", {"YANDEX_SEARCH_FOLDER_ID": "", "YANDEX_MODEL_URI": "gpt://folder-1/yandexgpt/latest"}):
            self.assertEqual(yandex_folder_id(), "folder-1")


if __name__ == "__main__":
    unittest.main()
