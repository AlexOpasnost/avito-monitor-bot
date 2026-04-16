"""Offline unit tests for the parser internals — no network required."""
import os
import sys

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("PROXY_LIST", "")
os.environ.setdefault("PROXY_ROTATE_URL", "")

from parser import (
    _parse_initial_data,
    _find_catalog_items,
    _ensure_sort_by_date,
    _extract_image_url,
    _extract_items_from_html,
    _looks_like_block,
)


def test_ensure_sort():
    assert _ensure_sort_by_date("https://www.avito.ru/moskva/kvartiry") == \
        "https://www.avito.ru/moskva/kvartiry?s=104"
    assert _ensure_sort_by_date("https://www.avito.ru/moskva/kvartiry?f=ABC") == \
        "https://www.avito.ru/moskva/kvartiry?f=ABC&s=104"
    # Already has s= -> unchanged
    u = "https://www.avito.ru/moskva/kvartiry?f=ABC&s=104"
    assert _ensure_sort_by_date(u) == u
    # Preserves base64 with - and _
    long_url = "https://www.avito.ru/x?f=ASg-_BC"
    assert _ensure_sort_by_date(long_url) == long_url + "&s=104"
    print("OK: _ensure_sort_by_date")


def test_find_catalog_items():
    data = {"catalog": {"items": [{"id": 1}, {"id": 2}]}}
    assert _find_catalog_items(data) == [{"id": 1}, {"id": 2}]
    nested = {"state": {"data": {"catalog": {"items": [{"id": 9}]}}}}
    assert _find_catalog_items(nested) == [{"id": 9}]
    # Should NOT pick up recommendations
    bad = {
        "recommendations": {"items": [{"id": 99}]},
        "catalog": {"items": [{"id": 1}]},
    }
    assert _find_catalog_items(bad) == [{"id": 1}]
    print("OK: _find_catalog_items")


def test_parse_initial_data():
    data = {
        "catalog": {
            "items": [
                {
                    "type": "item",
                    "value": {
                        "id": 12345,
                        "title": "iPhone 13",
                        "priceDetailed": {"value": 50000, "string": "50 000 ₽"},
                        "urlPath": "/moskva/telefony/iphone_12345",
                        "images": [{"864x648": "https://example.com/img.jpg"}],
                        "location": {"name": "Москва"},
                    },
                },
                # Should be filtered out — non-item type
                {"type": "xlItem", "value": {"id": 99999}},
                # Plain dict without wrapper — also valid
                {"id": 67890, "title": "Samsung", "price": 30000,
                 "urlPath": "/x/67890"},
            ]
        }
    }
    items = _parse_initial_data(data)
    assert items is not None
    assert len(items) == 2, f"expected 2 items, got {len(items)}"
    assert items[0].avito_id == "12345"
    assert items[0].title == "iPhone 13"
    assert items[0].price_value == 50000
    assert items[0].image_url == "https://example.com/img.jpg"
    assert items[0].location == "Москва"
    assert items[0].url == "https://www.avito.ru/moskva/telefony/iphone_12345"
    assert items[1].avito_id == "67890"
    assert items[1].price_value == 30000
    print("OK: _parse_initial_data")


def test_block_detection():
    assert _looks_like_block("Доступ ограничен: проблема с IP")
    assert _looks_like_block("Слишком много запросов")
    assert _looks_like_block("Robot Check")
    assert not _looks_like_block("Купить телефон в Москве")
    assert not _looks_like_block("")
    print("OK: _looks_like_block")


def test_extract_from_mfe_html():
    # Synthetic HTML mimicking Avito's modern hydration script
    payload = '{"i18n":{"hasMessages":{}},"state":{"data":{"catalog":{"items":[{"type":"item","value":{"id":777,"title":"Test phone","price":1500,"urlPath":"/x/777"}}]}}}}'
    # Avito HTML-escapes the JSON inside the script body
    escaped = payload.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = (
        '<html><body>'
        '<script type="mime/invalid" data-mfe-state="true">' + escaped + '</script>'
        '</body></html>'
    )
    items = _extract_items_from_html(html)
    assert items is not None and len(items) == 1, f"expected 1 item, got {items}"
    assert items[0].avito_id == "777"
    assert items[0].title == "Test phone"
    print("OK: _extract_items_from_html (mfe-state)")


def test_image_extraction():
    # variants shape
    val = {"images": [{"variants": {"864x648": "https://example.com/v.jpg"}}]}
    assert _extract_image_url(val) == "https://example.com/v.jpg"
    # cover shape
    val = {"cover": {"url": "https://example.com/c.jpg"}}
    assert _extract_image_url(val) == "https://example.com/c.jpg"
    # plain string
    val = {"image": "https://example.com/x.png"}
    assert _extract_image_url(val) == "https://example.com/x.png"
    # nothing
    assert _extract_image_url({"id": 1}) is None
    print("OK: _extract_image_url")


def test_parse_url_encoded_string():
    # If hydration var is a URL-encoded JSON string
    raw = '%7B%22catalog%22%3A%7B%22items%22%3A%5B%7B%22id%22%3A1%2C%22title%22%3A%22T%22%2C%22urlPath%22%3A%22%2Fa%22%2C%22price%22%3A100%7D%5D%7D%7D'
    items = _parse_initial_data(raw)
    assert items is not None and len(items) == 1
    assert items[0].avito_id == "1"
    print("OK: url-encoded string parse")


if __name__ == "__main__":
    test_ensure_sort()
    test_find_catalog_items()
    test_parse_initial_data()
    test_block_detection()
    test_extract_from_mfe_html()
    test_image_extraction()
    test_parse_url_encoded_string()
    print("\nALL UNIT TESTS PASSED")
