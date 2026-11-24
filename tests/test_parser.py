from __future__ import annotations

import json
import unittest
from urllib.parse import quote

from avito import (
    AvitoBlockedError,
    NotSearchUrl,
    Listing,
    build_page_url,
    format_price,
    normalize_avito_url,
    parse_api_items,
    parse_listing_html,
    parse_search_html,
    robust_market_price,
    search_title,
)
from formatter import format_listing_caption, market_diff


def make_state() -> dict:
    return {
        "items": [
            {
                "type": "item",
                "value": {
                    "id": 123456789,
                    "title": "Xbox Series S",
                    "description": "Продаю бокс с двумя геймпадами. Торг возможен.",
                    "priceDetailed": {"string": "25 000 ₽", "value": 25000},
                    "urlPath": "/belgorod/igry/xbox_series_s_123456789",
                    "images": [
                        {
                            "urls": {
                                "640x480": "https://80.img.avito.st/image/1/one.640.jpg",
                                "1280x960": "https://80.img.avito.st/image/1/one.1280.jpg",
                            }
                        },
                        "https://80.img.avito.st/image/1/two.640.jpg",
                    ],
                    "sellerInfo": {
                        "name": "Иван",
                        "isPrivate": True,
                        "rating": {"score": 4.9, "reviewsCount": 12},
                    },
                    "sortTimeStamp": 1790000000000,
                },
            },
            {
                "type": "item",
                "value": {
                    "id": "987654321",
                    "title": "Xbox One &amp; игры",
                    "description": "Второе объявление",
                    "price": "12 000 ₽",
                    "urlPath": "/belgorod/igry/xbox_one_987654321",
                    "images": [],
                },
            },
        ]
    }


def make_search_html() -> str:
    encoded = quote(json.dumps(make_state(), ensure_ascii=False))
    return (
        "<html><head><title>Авито</title></head><body><div id='app'></div>"
        f'<script>window.__initialData__ = "{encoded}";</script>'
        "</body></html>"
    )


class UrlTests(unittest.TestCase):
    def test_normalize_removes_context(self) -> None:
        url = normalize_avito_url(
            "https://www.avito.ru/belgorod/igry?context=H4sIAverylong&q=xbox&f=ASgB"
        )
        self.assertNotIn("context", url)
        self.assertIn("q=xbox", url)
        self.assertIn("f=ASgB", url)

    def test_item_url_is_rejected(self) -> None:
        with self.assertRaises(NotSearchUrl):
            normalize_avito_url(
                "https://www.avito.ru/belgorod/igry/xbox_series_s_123456789?context=abc"
            )

    def test_foreign_host_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            normalize_avito_url("https://avito.ru.evil.example/search?q=xbox")

    def test_page_url_sorts_by_date(self) -> None:
        url = build_page_url("https://www.avito.ru/belgorod?q=xbox&s=1", 2)
        self.assertIn("s=104", url)
        self.assertIn("p=2", url)
        self.assertNotIn("s=1&", url)

    def test_title_and_price(self) -> None:
        self.assertEqual(search_title("https://www.avito.ru/belgorod?q=xbox"), "Belgorod · xbox")
        self.assertEqual(format_price(25000), "25 000 ₽")


class ParserTests(unittest.TestCase):
    def test_parse_search(self) -> None:
        items = parse_search_html(make_search_html())
        self.assertEqual(len(items), 2)
        item = next(item for item in items if item.id == "123456789")
        self.assertEqual(item.title, "Xbox Series S")
        self.assertEqual(item.price, 25000)
        self.assertEqual(len(item.image_urls), 2)
        self.assertIn("one.1280.jpg", item.image_urls[0])
        self.assertTrue(item.seller_is_private)
        self.assertEqual(item.seller_rating, 4.9)
        self.assertEqual(item.seller_reviews, 12)
        self.assertEqual(item.url, "https://www.avito.ru/belgorod/igry/xbox_series_s_123456789")

        second = next(item for item in items if item.id == "987654321")
        self.assertEqual(second.title, "Xbox One & игры")
        self.assertEqual(second.price, 12000)

    def test_parse_detail_and_global_seller(self) -> None:
        detail = {
            "props": {
                "item": {
                    "id": 123456789,
                    "title": "Xbox Series S",
                    "description": "Полное описание",
                    "priceDetailed": {"string": "24 000 ₽", "value": 24000},
                    "urlPath": "/belgorod/igry/xbox_series_s_123456789",
                    "images": [{"urls": {"640x480": "https://80.img.avito.st/image/1/a.jpg"}}],
                },
                "sellerInfo": {
                    "name": "Магазин игр",
                    "isPrivate": False,
                    "rating": {"value": 4.5, "count": 3},
                },
            }
        }
        html = f"<script>window.__initialData__ = \"{quote(json.dumps(detail, ensure_ascii=False))}\";</script>"
        item = parse_listing_html(html, "123456789")
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.price, 24000)
        self.assertEqual(item.description, "Полное описание")
        self.assertEqual(item.seller_name, "Магазин игр")
        self.assertFalse(item.seller_is_private)
        self.assertEqual(item.seller_rating, 4.5)
        self.assertEqual(item.seller_reviews, 3)

    def test_meta_fallback(self) -> None:
        html = (
            "<html><head>"
            "<meta property='og:title' content='Xbox Series S — Avito'>"
            "<meta property='og:description' content='Описание из meta'>"
            "<meta property='og:image' content='https://80.img.avito.st/image/1/a.jpg'>"
            "<meta property='product:price:amount' content='19990'>"
            "</head></html>"
        )
        item = parse_listing_html(html, "123456789")
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.title, "Xbox Series S")
        self.assertEqual(item.price, 19990)
        self.assertEqual(item.description, "Описание из meta")
        self.assertEqual(len(item.image_urls), 1)

    def test_firewall(self) -> None:
        with self.assertRaises(AvitoBlockedError):
            parse_search_html("<html><div class='firewall__title'>Доступ ограничен</div></html>")

    def test_empty_search(self) -> None:
        self.assertEqual(parse_search_html("<html><body>Ничего не найдено</body></html>"), [])

    def test_market(self) -> None:
        self.assertEqual(robust_market_price([10000, 20000, 30000]), 20000)
        self.assertIsNone(robust_market_price([10000, 20000]))
        self.assertEqual(market_diff(25000, 38000), -34)


class FormatterTests(unittest.TestCase):
    def test_caption(self) -> None:
        item = Listing(
            id="1",
            title="Xbox Series S",
            url="https://www.avito.ru/item_1",
            price=25000,
            price_text="25 000 ₽",
            description="Продаю <бокс> & геймпады",
            seller_is_private=True,
            seller_reviews=0,
        )
        caption = format_listing_caption(item, 38000)
        self.assertIn("📦", caption)
        self.assertIn("💳 Цена: 25 000 ₽", caption)
        self.assertIn("🟢 Рынок: 38 000 ₽ (-34% к рынку)", caption)
        self.assertIn("👤 Продавец: частное лицо", caption)
        self.assertIn("⭐️ Рейтинг: Нет отзывов", caption)
        self.assertIn("⚠️ Заметки: дешевле рынка на 34%", caption)
        self.assertIn("&lt;бокс&gt; &amp; геймпады", caption)
        self.assertLessEqual(len(caption), 1024)

    def test_long_caption_is_truncated(self) -> None:
        item = Listing(
            id="1",
            title="X" * 300,
            url="https://www.avito.ru/item_1",
            price=25000,
            description="Очень длинное описание " * 300,
        )
        caption = format_listing_caption(item, None)
        self.assertLessEqual(len(caption), 1024)
        self.assertIn("</i>", caption)


if __name__ == "__main__":
    unittest.main()
