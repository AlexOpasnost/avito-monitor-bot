"""Offline unit tests for the cloudscraper parser internals."""
import os

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("PROXY_LIST", "")
os.environ.setdefault("PROXY_ROTATE_URL", "")

from parser import _extract_items_from_json, _parse_api_item


def test_parse_api_item_full():
    val = {
        "id": 12345,
        "title": "iPhone 13",
        "priceDetailed": {"value": 50000, "string": "50 000 ₽"},
        "urlPath": "/moskva/telefony/iphone_12345",
        "images": [{"864x648": "https://example.com/img.jpg"}],
        "location": {"name": "Москва"},
    }
    item = _parse_api_item(val)
    assert item.avito_id == "12345"
    assert item.title == "iPhone 13"
    assert item.price_value == 50000
    assert item.url == "https://www.avito.ru/moskva/telefony/iphone_12345"
    assert item.image_url == "https://example.com/img.jpg"
    assert item.location == "Москва"
    print("OK: _parse_api_item (full shape)")


def test_parse_api_item_plain_price():
    val = {"id": 7, "title": "X", "price": 1000, "urlPath": "/x/7"}
    item = _parse_api_item(val)
    assert item.avito_id == "7"
    assert item.price_value == 1000
    print("OK: _parse_api_item (plain int price)")


def test_extract_items_from_json_direct_catalog():
    data = {"catalog": {"items": [
        {"value": {"id": 1, "title": "A", "urlPath": "/a", "price": 100}},
        {"value": {"id": 2, "title": "B", "urlPath": "/b", "price": 200}},
        {"value": {"id": 3, "title": "C", "urlPath": "/c", "price": 300}},
    ]}}
    items = _extract_items_from_json(data)
    assert len(items) == 3
    assert items[0].avito_id == "1"
    print("OK: _extract_items_from_json (catalog.items)")


def test_extract_items_from_json_nested():
    # _extract_items_from_json recurses into dict values > 1000 chars,
    # so pad the nested block so the recursion guard is satisfied.
    filler = {"description": "x" * 1500}
    data = {
        "state": {"data": {"catalog": {"items": [
            {"value": {"id": 10, "urlPath": "/x/10", "price": 1, **filler}},
            {"value": {"id": 11, "urlPath": "/x/11", "price": 1, **filler}},
            {"value": {"id": 12, "urlPath": "/x/12", "price": 1, **filler}},
        ]}}},
    }
    items = _extract_items_from_json(data)
    assert len(items) == 3, f"got {len(items)}"
    assert {i.avito_id for i in items} == {"10", "11", "12"}
    print("OK: _extract_items_from_json (nested)")


def test_parse_item_image_variants():
    # variants shape
    val = {
        "id": 1, "urlPath": "/x", "price": 100,
        "images": [{"variants": {"864x648": "https://example.com/v.jpg"}}],
    }
    item = _parse_api_item(val)
    assert item.image_url == "https://example.com/v.jpg"
    print("OK: image from images[0].variants")


if __name__ == "__main__":
    test_parse_api_item_full()
    test_parse_api_item_plain_price()
    test_extract_items_from_json_direct_catalog()
    test_extract_items_from_json_nested()
    test_parse_item_image_variants()
    print("\nALL UNIT TESTS PASSED")
