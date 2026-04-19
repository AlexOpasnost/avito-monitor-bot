"""Offline unit tests — no network required.

Tests the Avito plugin's pure-python helpers + the dispatcher contract.
"""
import os

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("PROXY_LIST", "")
os.environ.setdefault("PROXY_ROTATE_URL", "")

from parsers import SearchItem, detect_source, supported_sources
from parsers.avito import (
    AvitoSource,
    _city_from_url_path,
    _extract_image_url,
    _extract_location,
    _parse_item,
)


# ---------- dispatcher ----------

def test_dispatcher_matches_avito():
    s = detect_source("https://www.avito.ru/moskva/kvartiry")
    assert s is not None and s.name == "avito"
    s = detect_source("https://m.avito.ru/all/foo")
    assert s is not None and s.name == "avito"
    print("OK: dispatcher matches avito")


def test_dispatcher_rejects_unknown():
    assert detect_source("https://example.com/anything") is None
    assert detect_source("") is None
    assert detect_source(None) is None
    print("OK: dispatcher rejects unknown")


def test_supported_sources():
    sources = supported_sources()
    assert "avito" in sources
    print(f"OK: supported sources = {sources}")


# ---------- avito plugin internals ----------

def test_avito_source_matches():
    src = AvitoSource()
    assert src.matches("https://www.avito.ru/x")
    assert src.matches("https://m.avito.ru/x")
    assert not src.matches("https://kufar.by/x")
    print("OK: AvitoSource.matches")


def test_parse_item_full():
    val = {
        "id": 12345,
        "title": "iPhone 13",
        "priceDetailed": {"value": 50000, "string": "50 000 ₽"},
        "urlPath": "/moskva/telefony/iphone_12345",
        "images": [{"864x864": "https://avito.st/image/x.jpg"}],
        "location": {"name": "Москва"},
    }
    item = _parse_item(val)
    assert isinstance(item, SearchItem)
    assert item.source == "avito"
    assert item.external_id == "12345"
    assert item.title == "iPhone 13"
    assert item.price_value == 50000
    assert item.url == "https://www.avito.ru/moskva/telefony/iphone_12345"
    assert item.image_url == "https://avito.st/image/x.jpg"
    assert item.location == "Москва"
    print("OK: _parse_item full shape")


def test_parse_item_plain_price():
    val = {"id": 7, "title": "X", "price": 1000, "urlPath": "/x/7"}
    item = _parse_item(val)
    assert item.external_id == "7"
    assert item.price_value == 1000
    print("OK: _parse_item plain int price")


def test_image_extraction():
    # variants shape
    val = {
        "id": 1, "urlPath": "/x", "price": 1,
        "images": [{"variants": {"864x648": "https://avito.st/v.jpg"}}],
    }
    assert _extract_image_url(val) == "https://avito.st/v.jpg"
    # Strict: no .jpg, no avito host -> rejected
    val = {"images": [{"url": "https://example.com/share/123"}]}
    assert _extract_image_url(val) is None
    print("OK: _extract_image_url (strict + variants)")


def test_location_from_path():
    assert _city_from_url_path("/moskva/foo/bar") == "Москва"
    assert _city_from_url_path("/sankt-peterburg/x") == "Санкт-Петербург"
    assert _city_from_url_path("/balakovo/x") == "Балаково"
    # Unknown slug -> Title Case
    assert _city_from_url_path("/xxx-yyy/x") == "Xxx yyy"
    # /all/ — country-wide
    assert _city_from_url_path("/all/foo") is None
    print("OK: _city_from_url_path")


def test_location_fallback():
    val = {"urlPath": "/moskva/odezhda/x", "location": {}}
    assert _extract_location(val) == "Москва"
    val = {"location": {"name": "Самара"}}
    assert _extract_location(val) == "Самара"
    val = {"geo": {"formattedAddress": "Москва, метро Арбатская"}}
    assert _extract_location(val) == "Москва, метро Арбатская"
    print("OK: _extract_location fallback chain")


if __name__ == "__main__":
    test_dispatcher_matches_avito()
    test_dispatcher_rejects_unknown()
    test_supported_sources()
    test_avito_source_matches()
    test_parse_item_full()
    test_parse_item_plain_price()
    test_image_extraction()
    test_location_from_path()
    test_location_fallback()
    print("\nALL UNIT TESTS PASSED")
