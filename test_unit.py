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
from parsers.kufar import (
    KufarSource,
    _extract_description as _kufar_description,
    _extract_image_url as _kufar_image,
    _extract_location as _kufar_location,
    _extract_price as _kufar_price,
    _extract_seller as _kufar_seller,
    _parse_item as _kufar_parse_item,
    _parse_list_time as _kufar_parse_time,
)


# ---------- dispatcher ----------

def test_dispatcher_matches_avito():
    s = detect_source("https://www.avito.ru/moskva/kvartiry")
    assert s is not None and s.name == "avito"
    s = detect_source("https://m.avito.ru/all/foo")
    assert s is not None and s.name == "avito"
    print("OK: dispatcher matches avito")


def test_dispatcher_matches_kufar():
    s = detect_source("https://www.kufar.by/l?cur=BYR&prc=r%3A0%2C500&sort=lst.d")
    assert s is not None and s.name == "kufar"
    s = detect_source("https://kufar.by/l/minsk")
    assert s is not None and s.name == "kufar"
    print("OK: dispatcher matches kufar")


def test_dispatcher_rejects_unknown():
    assert detect_source("https://example.com/anything") is None
    assert detect_source("") is None
    assert detect_source(None) is None
    print("OK: dispatcher rejects unknown")


def test_supported_sources():
    sources = supported_sources()
    assert "avito" in sources
    assert "kufar" in sources
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


# ---------- kufar plugin internals ----------

def test_kufar_source_matches():
    src = KufarSource()
    assert src.matches("https://www.kufar.by/l?cur=BYR")
    assert src.matches("https://kufar.by/item/123")
    assert not src.matches("https://www.avito.ru/x")
    assert not src.matches("")
    print("OK: KufarSource.matches")


def _kufar_sample_ad():
    return {
        "ad_id": 1065501472,
        "list_id": 1065501472,
        "ad_link": "https://www.kufar.by/item/1065501472",
        "subject": "Зонтики для фасада",
        "body": "Полный комплект, остались 20 штук.",
        "body_short": "Полный комплект",
        "price_byn": "50",
        "price_usd": "18",
        "currency": "BYR",
        "list_time": "2026-04-19T17:54:08Z",
        "images": [{
            "id": "0000", "media_storage": "rms",
            "path": "adim1/22bd529f-ba17-49ef-a52d-fd79a2ff341d.jpg",
            "yams_storage": False,
        }],
        "ad_parameters": [
            {"pl": "Регион", "vl": "Минская обл.", "p": "region", "v": 5, "pu": "rgn"},
            {"pl": "Город / Район", "vl": "Столбцы", "p": "area", "v": "104", "pu": "ar"},
        ],
        "account_parameters": [
            {"pl": "Имя", "vl": "", "p": "name", "v": "Александр", "pu": "nme"},
        ],
    }


def test_kufar_parse_item_full():
    item = _kufar_parse_item(_kufar_sample_ad())
    assert isinstance(item, SearchItem)
    assert item.source == "kufar"
    assert item.external_id == "1065501472"
    assert item.title == "Зонтики для фасада"
    assert item.price_value == 50
    assert "50 Br" in item.price and "18 $" in item.price
    assert item.url == "https://www.kufar.by/item/1065501472"
    assert item.image_url == (
        "https://rms.kufar.by/v1/list_thumbs_2x/"
        "adim1/22bd529f-ba17-49ef-a52d-fd79a2ff341d.jpg"
    )
    assert item.location == "Минская обл., Столбцы"
    assert item.description == "Полный комплект, остались 20 штук."
    assert item.seller_name == "Александр"
    # "2026-04-19T17:54:08Z" == 1776621248
    assert item.published_timestamp == 1776621248
    print("OK: kufar _parse_item full shape")


def test_kufar_price_forms():
    # BYN only
    s, v = _kufar_price({"price_byn": "1500", "price_usd": "0"})
    assert v == 1500 and s == "1 500 Br"
    # BYN + USD
    s, v = _kufar_price({"price_byn": "2000", "price_usd": "700"})
    assert v == 2000 and "2 000 Br" in s and "700 $" in s
    # USD only (rare but possible)
    s, v = _kufar_price({"price_byn": "", "price_usd": "100"})
    assert v == 100 and s == "100 $"
    # None
    s, v = _kufar_price({"price_byn": None, "price_usd": None})
    assert v is None and s == "Цена не указана"
    # Empty strings
    s, v = _kufar_price({"price_byn": "", "price_usd": ""})
    assert v is None and s == "Цена не указана"
    print("OK: kufar price forms")


def test_kufar_image_extraction():
    ad = _kufar_sample_ad()
    assert _kufar_image(ad).startswith(
        "https://rms.kufar.by/v1/list_thumbs_2x/adim1/"
    )
    assert _kufar_image({"images": []}) is None
    assert _kufar_image({"images": None}) is None
    assert _kufar_image({}) is None
    assert _kufar_image({"images": [{"media_storage": "rms"}]}) is None  # no path
    print("OK: kufar _extract_image_url")


def test_kufar_location_extraction():
    # region + area
    ad = _kufar_sample_ad()
    assert _kufar_location(ad) == "Минская обл., Столбцы"
    # dedupe when region == area (Минск/Минск case)
    dup = {"ad_parameters": [
        {"p": "region", "vl": "Минск"},
        {"p": "area", "vl": "Минск"},
    ]}
    assert _kufar_location(dup) == "Минск"
    # only region
    only_region = {"ad_parameters": [{"p": "region", "vl": "Минск"}]}
    assert _kufar_location(only_region) == "Минск"
    # missing
    assert _kufar_location({}) is None
    assert _kufar_location({"ad_parameters": []}) is None
    print("OK: kufar _extract_location")


def test_kufar_description_fallback():
    assert _kufar_description({"body": "hello"}) == "hello"
    assert _kufar_description({"body": None, "body_short": "short"}) == "short"
    assert _kufar_description({"body": "", "body_short": ""}) is None
    assert _kufar_description({}) is None
    print("OK: kufar _extract_description fallback")


def test_kufar_seller():
    assert _kufar_seller({
        "account_parameters": [{"p": "name", "v": "Алиса"}]
    }) == "Алиса"
    # no name param
    assert _kufar_seller({"account_parameters": [{"p": "phone", "v": "+375..."}]}) is None
    assert _kufar_seller({"account_parameters": []}) is None
    assert _kufar_seller({}) is None
    print("OK: kufar _extract_seller")


def test_kufar_time_parsing():
    # ISO Z format (observed)
    assert _kufar_parse_time("2026-04-19T17:54:08Z") == 1776621248
    # int seconds
    assert _kufar_parse_time(1776621248) == 1776621248
    # int milliseconds
    assert _kufar_parse_time(1776621248000) == 1776621248
    # empty / None
    assert _kufar_parse_time(None) is None
    assert _kufar_parse_time("") is None
    assert _kufar_parse_time("garbage") is None
    print("OK: kufar _parse_list_time")


if __name__ == "__main__":
    test_dispatcher_matches_avito()
    test_dispatcher_matches_kufar()
    test_dispatcher_rejects_unknown()
    test_supported_sources()
    test_avito_source_matches()
    test_parse_item_full()
    test_parse_item_plain_price()
    test_image_extraction()
    test_location_from_path()
    test_location_fallback()
    test_kufar_source_matches()
    test_kufar_parse_item_full()
    test_kufar_price_forms()
    test_kufar_image_extraction()
    test_kufar_location_extraction()
    test_kufar_description_fallback()
    test_kufar_seller()
    test_kufar_time_parsing()
    print("\nALL UNIT TESTS PASSED")
