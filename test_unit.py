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
from parsers.mercari import (
    MercariSource,
    _extract_keyword as _mer_extract_keyword,
    _format_jpy_price as _mer_format_price,
    _parse_item as _mer_parse_item,
)
from parsers.olx import (
    OlxSource,
    _clean_description as _olx_clean_desc,
    _encode_pairs as _olx_encode_pairs,
    _extract_photo as _olx_extract_photo,
    _first_organic_card_id as _olx_first_organic_card_id,
    _parse_api_item as _olx_parse_api_item,
    _parse_api_response as _olx_parse_api_response,
    _parse_iso as _olx_parse_iso,
    _parse_location as _olx_parse_location,
    _parse_price as _olx_parse_price,
    _parse_raw_query as _olx_parse_raw_query,
    _usd_estimate as _olx_usd_estimate,
)


# ---------- dispatcher ----------

def test_proxy_for_source():
    """Per-source proxy routing: Avito/Kufar go through mobile proxy when
    one is configured, OLX/Vinted/Mercari always go direct."""
    from parsers import common as _common
    from parsers.common import proxy_for_source

    orig = _common.config.proxy_list
    try:
        # With a proxy configured
        _common.config.proxy_list = ["http://user:pass@mproxy.site:17751"]
        assert proxy_for_source("avito") == "http://user:pass@mproxy.site:17751"
        assert proxy_for_source("kufar") == "http://user:pass@mproxy.site:17751"
        assert proxy_for_source("olx") is None
        assert proxy_for_source("vinted") is None
        assert proxy_for_source("mercari") is None
        assert proxy_for_source("") is None
        assert proxy_for_source(None) is None
        # With no proxy configured — even proxied sources get None
        _common.config.proxy_list = []
        assert proxy_for_source("avito") is None
        assert proxy_for_source("kufar") is None
        assert proxy_for_source("olx") is None
    finally:
        _common.config.proxy_list = orig
    print("OK: proxy_for_source per-source routing")


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


def test_dispatcher_matches_mercari():
    s = detect_source("https://jp.mercari.com/search?keyword=iphone")
    assert s is not None and s.name == "mercari"
    s = detect_source("https://www.mercari.com/search?q=camera")
    assert s is not None and s.name == "mercari"
    print("OK: dispatcher matches mercari")


def test_dispatcher_matches_olx():
    s = detect_source("https://www.olx.pl/oferty/q-iphone/?search%5Border%5D=created_at%3Adesc")
    assert s is not None and s.name == "olx"
    s = detect_source("https://www.olx.ua/d/uk/list/")
    assert s is not None and s.name == "olx"
    s = detect_source("https://www.olx.com.br/celulares")
    assert s is not None and s.name == "olx"
    print("OK: dispatcher matches olx")


def test_dispatcher_rejects_unknown():
    assert detect_source("https://example.com/anything") is None
    assert detect_source("") is None
    assert detect_source(None) is None
    print("OK: dispatcher rejects unknown")


def test_supported_sources():
    sources = supported_sources()
    assert "avito" in sources
    assert "kufar" in sources
    assert "olx" in sources
    assert "mercari" in sources
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


# ---------- olx plugin internals ----------

def test_olx_source_matches():
    src = OlxSource()
    assert src.matches("https://www.olx.pl/oferty/q-iphone")
    assert src.matches("https://olx.ua/d/list")
    assert src.matches("https://m.olx.ro/anunturi")
    assert src.matches("https://www.olx.com.br/celulares")
    assert not src.matches("https://www.avito.ru/x")
    assert not src.matches("https://www.kufar.by/x")
    assert not src.matches("")
    print("OK: OlxSource.matches")


def test_mercari_source_matches():
    src = MercariSource()
    assert src.matches("https://jp.mercari.com/search?keyword=iphone")
    assert src.matches("https://www.mercari.com/")
    assert not src.matches("https://www.avito.ru/x")
    assert not src.matches("")
    print("OK: MercariSource.matches")


def test_mercari_extract_keyword():
    # Primary param
    assert _mer_extract_keyword(
        "https://jp.mercari.com/search?keyword=iphone+15"
    ) == "iphone 15"
    # URL-encoded Japanese
    assert _mer_extract_keyword(
        "https://jp.mercari.com/search?keyword=%E3%82%B9%E3%83%9E%E3%83%9B"
    ) == "スマホ"
    # Fallback `q=` / `query=`
    assert _mer_extract_keyword(
        "https://jp.mercari.com/search?q=camera"
    ) == "camera"
    # Empty / missing
    assert _mer_extract_keyword("https://jp.mercari.com/search") is None
    assert _mer_extract_keyword("") is None
    print("OK: _mer_extract_keyword")


def test_mercari_format_jpy_price():
    assert _mer_format_price(3999) == "3 999 ¥ (~26 $)"
    # Small amount where USD rounds to 0 → drop USD hint
    assert _mer_format_price(50) == "50 ¥"
    assert _mer_format_price(None) == "Цена не указана"
    assert _mer_format_price(0) == "Цена не указана"
    assert _mer_format_price(-100) == "Цена не указана"
    print("OK: _mer_format_jpy_price")


def test_mercari_parse_item():
    # Fake item mimicking mercapi's SearchResultItem shape
    from datetime import datetime
    class FakeItem:
        id_ = "m71928728136"
        name = "Apple iPhone 15 ブラック 128GB"
        price = 69000
        is_no_price = False
        thumbnails = [
            "https://static.mercdn.net/thumb/item/webp/m71928728136_1.jpg?123",
        ]
        item_type = "ITEM_TYPE_MERCARI"
        status = "ITEM_STATUS_ON_SALE"
        created = datetime(2026, 4, 23, 14, 22, 20)  # naive — any system-local
        updated = datetime(2026, 4, 23, 15, 10, 0)

    it = _mer_parse_item(FakeItem())
    assert it.source == "mercari"
    assert it.external_id == "m71928728136"
    assert it.title == "Apple iPhone 15 ブラック 128GB"
    assert it.price_value == 69000
    assert "69 000 ¥" in it.price and "$" in it.price
    assert it.url == "https://jp.mercari.com/item/m71928728136"
    assert it.image_url and it.image_url.startswith(
        "https://static.mercdn.net/thumb/"
    )
    # published_timestamp prefers `updated` — the bigger of the two
    assert it.published_timestamp is not None
    assert it.published_timestamp == int(FakeItem.updated.timestamp())
    assert it.location is None  # search results carry no region
    print("OK: mercari _parse_item full shape")


def test_olx_parse_iso():
    # API returns times like "2026-04-20T18:55:59+02:00" — tz-aware.
    # 18:55:59 +02:00 = 16:55:59 UTC = 1776704159
    assert _olx_parse_iso("2026-04-20T18:55:59+02:00") == 1776704159
    # Z-suffix form (same absolute moment, UTC-local)
    assert _olx_parse_iso("2026-04-20T16:55:59Z") == 1776704159
    # None / empty / junk
    assert _olx_parse_iso(None) is None
    assert _olx_parse_iso("") is None
    assert _olx_parse_iso("garbage") is None
    print("OK: _olx_parse_iso")


def test_olx_parse_price():
    # Standard shape from /api/v1/offers
    params = [
        {"key": "category", "value": {"label": "Computers"}},
        {"key": "price", "value": {
            "value": 5400.0, "currency": "PLN",
            "label": "5 400 zł", "negotiable": False, "arranged": False,
        }},
    ]
    label, value, cur = _olx_parse_price(params)
    assert label == "5 400 zł"
    assert value == 5400
    assert cur == "PLN"

    # Negotiable flag decorates the label
    params[1]["value"]["negotiable"] = True
    label, _, _ = _olx_parse_price(params)
    assert "до торга" in label

    # Arranged-only price (no number)
    params = [{"key": "price", "value": {"arranged": True, "label": ""}}]
    label, value, _ = _olx_parse_price(params)
    assert label == "Договорная"

    # Missing price param
    assert _olx_parse_price([{"key": "state", "value": {"key": "new"}}]) == (
        "Цена не указана", None, None,
    )
    print("OK: _olx_parse_price")


def test_olx_usd_estimate():
    # PLN 5400 → ~$1350
    usd = _olx_usd_estimate(5400, "PLN")
    assert usd and 1200 <= usd <= 1500
    # EUR stays close
    usd = _olx_usd_estimate(100, "EUR")
    assert usd and 100 <= usd <= 120
    # USD stays identical
    assert _olx_usd_estimate(100, "USD") == 100
    # Unknown currency -> None
    assert _olx_usd_estimate(100, "XXX") is None
    # Zero or None-ish
    assert _olx_usd_estimate(0, "PLN") is None
    print("OK: _olx_usd_estimate")


def test_olx_parse_location():
    # City + region
    loc = {
        "city": {"name": "Warszawa"}, "region": {"name": "Mazowieckie"},
    }
    assert _olx_parse_location(loc) == "Warszawa, Mazowieckie"
    # City + district + region
    loc = {
        "city": {"name": "Warszawa"},
        "district": {"name": "Mokotów"},
        "region": {"name": "Mazowieckie"},
    }
    assert _olx_parse_location(loc) == "Warszawa, Mokotów, Mazowieckie"
    # Deduplicates when city == region (e.g. Warszawa/Warszawa)
    loc = {"city": {"name": "Warszawa"}, "region": {"name": "Warszawa"}}
    assert _olx_parse_location(loc) == "Warszawa"
    # Only region
    assert _olx_parse_location({"region": {"name": "Silesia"}}) == "Silesia"
    # Empty
    assert _olx_parse_location({}) is None
    print("OK: _olx_parse_location")


def test_olx_extract_photo():
    # Template URL with {width}x{height} placeholders
    photos = [{"link": "https://ireland.apollo.olxcdn.com:443/v1/files/abc-PL/image;s={width}x{height}"}]
    url = _olx_extract_photo(photos)
    assert url and "600x600" in url and "{width}" not in url

    # Empty list
    assert _olx_extract_photo([]) is None

    # Link missing
    assert _olx_extract_photo([{"id": 1}]) is None

    # Non-http link → rejected
    assert _olx_extract_photo([{"link": "/static/placeholder.svg"}]) is None
    print("OK: _olx_extract_photo")


def test_olx_clean_description():
    raw = "<strong>Hello</strong> world<br />next line"
    assert _olx_clean_desc(raw) == "Hello world\nnext line"

    # Entities decode
    assert _olx_clean_desc("a &amp; b") == "a & b"

    # <p> becomes paragraph break; consecutive newlines collapse
    assert _olx_clean_desc("<p>a</p><p>b</p>") == "a\n\nb"

    # Non-string input
    assert _olx_clean_desc(None) == ""  # type: ignore[arg-type]
    print("OK: _olx_clean_description")


def test_olx_raw_query_encoding_roundtrip():
    # Parse a realistic OLX query string with bracket notation, then
    # re-encode, and make sure the result the API would actually accept
    # keeps the brackets and `:` separators.
    pairs = _olx_parse_raw_query(
        "search%5Bfilter_enum_state%5D%5B0%5D=new"
        "&search%5Border%5D=created_at%3Adesc"
    )
    assert ("search[filter_enum_state][0]", "new") in pairs
    assert ("search[order]", "created_at:desc") in pairs

    enc = _olx_encode_pairs(pairs)
    assert "search[filter_enum_state][0]=new" in enc
    # `:` is preserved inside the value
    assert "created_at:desc" in enc
    print("OK: _olx raw-query parse + encode")


def _olx_api_sample_item():
    """Snapshot of a real API response entry with all the fields we care
    about populated. Feeds _parse_api_item for a shape-level check."""
    return {
        "id": 1068316812,
        "url": "https://www.olx.pl/d/oferta/apple-iphone-13-pro-CID99-ID1aiy0k.html",
        "title": "Apple iPhone 13 Pro Sierra Blue 128 GB",
        "last_refresh_time": "2026-04-20T18:31:53+02:00",
        "created_time": "2026-04-20T18:26:27+02:00",
        "description": "<strong>Sprzedam</strong><br />w super stanie",
        "promotion": {"top_ad": False},
        "params": [
            {"key": "price", "value": {
                "value": 2500.0, "currency": "PLN",
                "label": "2 500 zł", "negotiable": False, "arranged": False,
            }},
        ],
        "user": {"name": "Michał"},
        "location": {
            "city": {"name": "Warszawa"}, "region": {"name": "Mazowieckie"},
        },
        "photos": [
            {"link": "https://ireland.apollo.olxcdn.com:443/v1/files/foo-PL/image;s={width}x{height}"},
            {"link": "https://ireland.apollo.olxcdn.com:443/v1/files/bar-PL/image;s={width}x{height}"},
        ],
    }


def test_olx_parse_api_item_full_shape():
    item = _olx_parse_api_item(_olx_api_sample_item())
    assert item.source == "olx"
    assert item.external_id == "1068316812"
    assert item.title == "Apple iPhone 13 Pro Sierra Blue 128 GB"
    # Price has USD hint appended (PLN 2500 ≈ $625)
    assert "2 500 zł" in item.price and "$" in item.price
    assert item.price_value == 2500
    assert item.url.endswith(".html")
    assert item.location == "Warszawa, Mazowieckie"
    assert item.description == "Sprzedam\nw super stanie"
    assert item.seller_name == "Michał"
    assert item.image_url and "600x600" in item.image_url
    # 2026-04-20T18:31:53+02:00 → 16:31:53 UTC → 1776702713
    assert item.published_timestamp == 1776702713
    print("OK: olx _parse_api_item full shape")


def test_olx_first_organic_card_id():
    # Mix: first card is promoted, second is organic — expect second id.
    html = """
<div data-cy="l-card" id="111">
  <a href="/d/oferta/promo-CID99-IDa.html?search_reason=search%7Cpromoted">promo</a>
</div>
<div data-cy="l-card" id="222">
  <a href="/d/oferta/org-CID3102-IDb.html?search_reason=search%7Corganic">real</a>
</div>
<div data-cy="l-card" id="333">
  <a href="/d/oferta/org-CID3102-IDc.html?search_reason=search%7Corganic">real</a>
</div>
"""
    assert _olx_first_organic_card_id(html) == 222

    # All promoted → None
    html_all_promo = """
<div data-cy="l-card" id="111">
  <a href="/d/oferta/a.html?search_reason=search%7Cpromoted">x</a>
</div>
<div data-cy="l-card" id="112">
  <a href="/d/oferta/b.html?search_reason=search|promoted">x</a>
</div>
"""
    assert _olx_first_organic_card_id(html_all_promo) is None

    # No l-cards at all → None
    assert _olx_first_organic_card_id("<html>nothing here</html>") is None
    print("OK: _olx_first_organic_card_id skips promoted")


def test_olx_parse_api_response_filters_promoted():
    resp = {
        "data": [
            {
                "id": 111, "title": "Promoted Car",
                "url": "https://www.olx.pl/d/oferta/a-CID5-IDxxx.html",
                "last_refresh_time": "2026-04-20T10:00:00+02:00",
                "promotion": {"top_ad": True},
                "params": [{"key": "price", "value": {
                    "value": 1000, "currency": "PLN", "label": "1 000 zł",
                }}],
                "photos": [{"link": "https://ireland.apollo.olxcdn.com:443/v1/files/a-PL/image;s={width}x{height}"}],
                "location": {"city": {"name": "Kraków"}},
            },
            {
                "id": 222, "title": "Organic iPhone",
                "url": "https://www.olx.pl/d/oferta/b-CID99-IDyyy.html",
                "last_refresh_time": "2026-04-20T11:00:00+02:00",
                "promotion": {"top_ad": False},
                "params": [{"key": "price", "value": {
                    "value": 2000, "currency": "PLN", "label": "2 000 zł",
                }}],
                "photos": [{"link": "https://ireland.apollo.olxcdn.com:443/v1/files/b-PL/image;s={width}x{height}"}],
                "location": {"city": {"name": "Poznań"}},
            },
            {
                "id": 333, "title": "Organic etui",
                "url": "https://www.olx.pl/d/oferta/c-CID99-IDzzz.html",
                "last_refresh_time": "2026-04-20T12:00:00+02:00",
                "promotion": {},
                "params": [{"key": "price", "value": {
                    "value": 30, "currency": "PLN", "label": "30 zł",
                }}],
                "photos": [{"link": "https://ireland.apollo.olxcdn.com:443/v1/files/c-PL/image;s={width}x{height}"}],
                "location": {"city": {"name": "Łódź"}},
            },
        ],
        "metadata": {
            "total_elements": 3,
            "visible_total_count": 3,
            "source": {"organic": [1, 2]},   # index 0 = promoted
        },
    }
    items = _olx_parse_api_response(resp, "https://www.olx.pl/oferty/q-iphone/")
    assert items is not None
    ids = [i.external_id for i in items]
    assert "111" not in ids, "promoted item should be filtered"
    assert "222" in ids and "333" in ids
    # Every organic item has photo + date + location
    for i in items:
        assert i.image_url is not None
        assert i.published_timestamp is not None
        assert i.location is not None
    print("OK: olx _parse_api_response filters promoted by top_ad + organic_idx")


if __name__ == "__main__":
    test_proxy_for_source()
    test_dispatcher_matches_avito()
    test_dispatcher_matches_kufar()
    test_dispatcher_matches_olx()
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
    test_dispatcher_matches_mercari()
    test_mercari_source_matches()
    test_mercari_extract_keyword()
    test_mercari_format_jpy_price()
    test_mercari_parse_item()
    test_olx_source_matches()
    test_olx_parse_iso()
    test_olx_parse_price()
    test_olx_usd_estimate()
    test_olx_parse_location()
    test_olx_extract_photo()
    test_olx_clean_description()
    test_olx_raw_query_encoding_roundtrip()
    test_olx_parse_api_item_full_shape()
    test_olx_first_organic_card_id()
    test_olx_parse_api_response_filters_promoted()
    print("\nALL UNIT TESTS PASSED")
