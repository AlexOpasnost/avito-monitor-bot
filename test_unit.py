"""Offline unit tests for the parser internals — no network required."""
import os
import sys

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("PROXY_LIST", "")
os.environ.setdefault("PROXY_ROTATE_URL", "")

from parser import (
    _ensure_sort_by_date,
    _extract_image_url,
    _extract_items_from_html,
    _items_from_raw_list,
    _looks_like_block,
    _walk_path,
    _MAIN_CATALOG_PATH,
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


def test_walk_path():
    data = {"state": {"data": {"catalog": {"items": [{"id": 1}]}}}}
    assert _walk_path(data, _MAIN_CATALOG_PATH) == [{"id": 1}]
    assert _walk_path(data, ("state", "data", "missing")) is None
    assert _walk_path({}, _MAIN_CATALOG_PATH) is None
    print("OK: _walk_path")


def test_items_from_raw_list():
    raw = [
        {
            "type": "item",
            "value": {
                "id": 12345, "title": "iPhone 13",
                "priceDetailed": {"value": 50000, "string": "50 000 ₽"},
                "urlPath": "/moskva/telefony/iphone_12345",
                "images": [{"864x864": "https://example.com/img.jpg"}],
                "location": {"name": "Москва"},
            },
        },
        # Should be filtered — non-item type
        {"type": "xlItem", "value": {"id": 99999}},
        # Plain dict (no wrapper)
        {"id": 67890, "title": "Samsung", "price": 30000, "urlPath": "/x/67890"},
    ]
    items = _items_from_raw_list(raw)
    assert len(items) == 2
    assert items[0].avito_id == "12345"
    assert items[0].image_url == "https://example.com/img.jpg"
    assert items[0].location == "Москва"
    assert items[1].avito_id == "67890"
    print("OK: _items_from_raw_list")


def test_only_main_catalog_extracted():
    """Critical: a recommendations block in the same page must NOT bleed
    into our items, even if it has an `items` array structurally identical
    to the catalog."""
    payload = {
        "i18n": {"hasMessages": {}},
        "state": {
            "data": {
                # Main search catalog — should be picked
                "catalog": {
                    "items": [
                        {"type": "item", "value": {"id": 100, "title": "Right item",
                                                    "urlPath": "/x/100", "price": 1}}
                    ]
                },
                # Sibling block with items — must be ignored
                "recommendations": {
                    "items": [
                        {"type": "item", "value": {"id": 999, "title": "WRONG dress",
                                                    "urlPath": "/x/999", "price": 1}}
                    ]
                },
                "viewedItems": {
                    "items": [
                        {"type": "item", "value": {"id": 888, "title": "Recently viewed",
                                                    "urlPath": "/x/888", "price": 1}}
                    ]
                },
            }
        },
    }
    escaped = orjson_dumps(payload).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = f'<html><body><script type="mime/invalid" data-mfe-state="true">{escaped}</script></body></html>'
    items = _extract_items_from_html(html)
    assert items is not None
    assert len(items) == 1, f"expected 1 item, got {len(items)}"
    assert items[0].avito_id == "100"
    assert "WRONG" not in items[0].title
    print("OK: only main catalog extracted (recs/viewed ignored)")


def orjson_dumps(obj):
    import orjson
    return orjson.dumps(obj).decode("utf-8")


def test_block_detection():
    assert _looks_like_block("Доступ ограничен: проблема с IP")
    assert _looks_like_block("Слишком много запросов")
    assert _looks_like_block("Robot Check")
    assert not _looks_like_block("Купить телефон в Москве")
    assert not _looks_like_block("")
    print("OK: _looks_like_block")


def test_extract_from_mfe_html():
    payload = '{"i18n":{"hasMessages":{}},"state":{"data":{"catalog":{"items":[{"type":"item","value":{"id":777,"title":"Test phone","price":1500,"urlPath":"/x/777"}}]}}}}'
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


if __name__ == "__main__":
    test_ensure_sort()
    test_walk_path()
    test_items_from_raw_list()
    test_only_main_catalog_extracted()
    test_block_detection()
    test_extract_from_mfe_html()
    test_image_extraction()
    print("\nALL UNIT TESTS PASSED")
