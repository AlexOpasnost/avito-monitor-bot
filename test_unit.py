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
from parsers.olx import (
    OlxSource,
    _absolutize as _olx_absolutize,
    _extract_items as _olx_extract_items,
    _is_promoted as _olx_is_promoted,
    _parse_relative_date as _olx_parse_date,
    _strip_search_reason as _olx_strip_search_reason,
    _tz_for_url as _olx_tz_for_url,
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


def test_olx_is_promoted():
    assert _olx_is_promoted("/d/oferta/foo.html?search_reason=search%7Cpromoted")
    assert _olx_is_promoted("/d/oferta/foo.html?search_reason=search|promoted")
    assert _olx_is_promoted("/d/oferta/foo.html?x=1&search_reason=search%7Cpromoted")
    assert not _olx_is_promoted("/d/oferta/foo.html?search_reason=search%7Corganic")
    assert not _olx_is_promoted("/d/oferta/foo.html?search_reason=search|organic")
    assert not _olx_is_promoted("/d/oferta/foo.html")
    print("OK: _olx_is_promoted")


def test_olx_strip_search_reason():
    u = _olx_strip_search_reason(
        "https://www.olx.pl/d/oferta/foo.html?search_reason=search%7Corganic"
    )
    assert u == "https://www.olx.pl/d/oferta/foo.html"
    # Other params preserved
    u = _olx_strip_search_reason(
        "https://www.olx.pl/d/oferta/foo.html?a=1&search_reason=search%7Corganic&b=2"
    )
    assert "a=1" in u and "b=2" in u and "search_reason" not in u
    # Plain URL unchanged
    u = _olx_strip_search_reason("https://www.olx.pl/d/oferta/foo.html")
    assert u == "https://www.olx.pl/d/oferta/foo.html"
    print("OK: _olx_strip_search_reason")


def test_olx_absolutize():
    u = _olx_absolutize("/d/oferta/foo.html?search_reason=search%7Corganic",
                        "https://www.olx.pl")
    assert u == "https://www.olx.pl/d/oferta/foo.html"
    # Already absolute
    u = _olx_absolutize(
        "https://www.olx.pl/d/oferta/foo.html?search_reason=search%7Corganic",
        "https://www.olx.pl",
    )
    assert u == "https://www.olx.pl/d/oferta/foo.html"
    # Href without leading slash
    u = _olx_absolutize("d/oferta/foo.html", "https://www.olx.pl")
    assert u == "https://www.olx.pl/d/oferta/foo.html"
    print("OK: _olx_absolutize")


def test_olx_tz_mapping():
    # Always returns a tzinfo-compatible object (ZoneInfo when tzdata is
    # available on the host, UTC fallback otherwise — both are valid for
    # runtime).
    from datetime import datetime
    for u in ("https://www.olx.pl/x", "https://www.olx.ua/x",
              "https://www.olx.com.br/x", "https://www.olx.xx/x"):
        tz = _olx_tz_for_url(u)
        # Must work as a tzinfo
        dt = datetime.now(tz)
        assert dt.tzinfo is tz
    print("OK: _olx_tz_for_url")


def test_olx_parse_relative_date():
    from datetime import datetime, timezone as tz_, timedelta
    # Use a fixed-offset tz so the test runs identically on any host
    # (Windows may lack tzdata for IANA zones).
    tz = tz_(timedelta(hours=2))

    # "Dzisiaj o 21:25"
    ts = _olx_parse_date("Dzisiaj o 21:25", tz)
    assert ts is not None
    dt = datetime.fromtimestamp(ts, tz)
    assert dt.hour == 21 and dt.minute == 25
    # It should be today's date in Warsaw tz
    today_pl = datetime.now(tz).date()
    assert dt.date() == today_pl

    # "Odświeżono dzisiaj o 11:02" also contains "dzisiaj"
    ts = _olx_parse_date("Odświeżono dzisiaj o 11:02", tz)
    assert ts is not None
    dt = datetime.fromtimestamp(ts, tz)
    assert dt.hour == 11 and dt.minute == 2

    # Ukrainian
    ts = _olx_parse_date("Сьогодні о 09:30", tz)
    assert ts is not None
    dt = datetime.fromtimestamp(ts, tz)
    assert dt.hour == 9 and dt.minute == 30

    # "Wczoraj o 23:00" = yesterday
    ts = _olx_parse_date("Wczoraj o 23:00", tz)
    assert ts is not None
    dt = datetime.fromtimestamp(ts, tz)
    assert dt.hour == 23 and dt.minute == 0
    yesterday = (datetime.now(tz) - timedelta(days=1)).date()
    assert dt.date() == yesterday

    # Polish absolute date "19 kwietnia 2026" (genitive form)
    ts = _olx_parse_date("19 kwietnia 2026", tz)
    assert ts is not None
    dt = datetime.fromtimestamp(ts, tz)
    assert dt.year == 2026 and dt.month == 4 and dt.day == 19

    # Polish month in nominative (rare but possible)
    ts = _olx_parse_date("3 kwiecień 2026", tz)
    assert ts is not None
    dt = datetime.fromtimestamp(ts, tz)
    assert dt.month == 4 and dt.day == 3

    # Ukrainian "19 квітня 2026"
    ts = _olx_parse_date("19 квітня 2026", tz)
    assert ts is not None
    dt = datetime.fromtimestamp(ts, tz)
    assert dt.month == 4 and dt.day == 19

    # Numeric "19.04.2026"
    ts = _olx_parse_date("19.04.2026", tz)
    assert ts is not None
    dt = datetime.fromtimestamp(ts, tz)
    assert dt.year == 2026 and dt.month == 4 and dt.day == 19

    # With HH:MM prefix: "15:32 19.04.2026"
    ts = _olx_parse_date("15:32 19.04.2026", tz)
    assert ts is not None
    dt = datetime.fromtimestamp(ts, tz)
    assert dt.hour == 15 and dt.minute == 32 and dt.day == 19

    # Unknown month word → None
    ts = _olx_parse_date("19 blornstag 2026", tz)
    assert ts is None

    # Empty
    assert _olx_parse_date("", tz) is None
    print("OK: _olx_parse_relative_date")


def test_olx_image_lazy_load_fallback():
    from bs4 import BeautifulSoup
    from parsers.olx import _extract_image_url

    # Case 1: src has real URL → use it
    html = '<div><img src="https://ireland.apollo.olxcdn.com:443/v1/files/abc-PL/image;s=216x152;q=50"/></div>'
    img_card = BeautifulSoup(html, "html.parser").select_one("div")
    url = _extract_image_url(img_card)
    assert url and url.startswith("https://ireland.apollo.olxcdn.com")
    assert ";s=512x512" in url

    # Case 2: src is placeholder SVG, data-src has real URL
    html = (
        '<div><img src="/app/static/media/no_thumbnail.15f456ec5.svg" '
        'data-src="https://ireland.apollo.olxcdn.com:443/v1/files/xyz-PL/image;s=216x152;q=50"/></div>'
    )
    img_card = BeautifulSoup(html, "html.parser").select_one("div")
    url = _extract_image_url(img_card)
    assert url and "xyz-PL" in url
    assert ";s=512x512" in url

    # Case 3: src is placeholder, srcset has real URL (most common for OLX)
    html = (
        '<div><img src="/app/static/media/no_thumbnail.15f456ec5.svg" '
        'srcset="https://ireland.apollo.olxcdn.com:443/v1/files/qqq-PL/image;s=150x188;q=50 150w, '
        'https://ireland.apollo.olxcdn.com:443/v1/files/qqq-PL/image;s=270x338;q=50 300w"/></div>'
    )
    img_card = BeautifulSoup(html, "html.parser").select_one("div")
    url = _extract_image_url(img_card)
    assert url and "qqq-PL" in url
    assert ";s=512x512" in url

    # Case 4: nothing usable → None
    html = '<div><img src="/app/static/media/no_thumbnail.15f456ec5.svg"/></div>'
    img_card = BeautifulSoup(html, "html.parser").select_one("div")
    assert _extract_image_url(img_card) is None

    # Case 5: no img at all
    html = '<div></div>'
    img_card = BeautifulSoup(html, "html.parser").select_one("div")
    assert _extract_image_url(img_card) is None
    print("OK: _olx image lazy-load fallbacks (src -> data-src -> srcset)")


def _olx_card_html(card_inner: str, card_id: str) -> str:
    return (
        f'<html><body>'
        f'<div data-cy="l-card" data-testid="l-card" id="{card_id}">'
        f'{card_inner}</div></body></html>'
    )


def test_olx_extract_items_organic_vs_promoted():
    # One promoted + two organic cards. Expect only 2 items, promoted filtered.
    html = """
<html><body>
<span data-testid="total-count">Znaleźliśmy ponad 1000 ogłoszeń</span>

<div data-cy="l-card" id="111">
  <a href="/d/oferta/promo-CID99-IDxxx.html?search_reason=search%7Cpromoted">
    <img src="https://ireland.apollo.olxcdn.com:443/v1/files/promo-PL/image;s=216x152;q=50"/>
  </a>
  <div data-cy="ad-card-title"><h4>Promoted Item</h4></div>
  <p data-testid="ad-price">100 zł</p>
  <p data-testid="location-date">Warszawa - Dzisiaj o 10:00</p>
</div>

<div data-cy="l-card" id="222">
  <a href="/d/oferta/organic1-CID99-IDaaa.html?search_reason=search%7Corganic">
    <img src="https://ireland.apollo.olxcdn.com:443/v1/files/org1-PL/image;s=216x152;q=50"/>
  </a>
  <div data-cy="ad-card-title"><h4>Organic One</h4></div>
  <p data-testid="ad-price">2 009 zł</p>
  <p data-testid="location-date">Poznań, Jeżyce - Dzisiaj o 11:02</p>
</div>

<div data-cy="l-card" id="333">
  <a href="/d/oferta/organic2-CID99-IDbbb.html?search_reason=search%7Corganic">
    <img src="/app/static/media/no_thumbnail.15f456ec5.svg"/>
  </a>
  <div data-cy="ad-card-title"><h4>Organic Two</h4></div>
  <p data-testid="ad-price">3 500 złdo negocjacji</p>
  <p data-testid="location-date">Katowice, Dąb - 03 kwietnia 2026</p>
</div>
</body></html>
"""
    items = _olx_extract_items(html, "https://www.olx.pl/oferty/q-iphone/")
    assert items is not None
    assert len(items) == 2, f"expected 2 organic, got {len(items)}"
    ids = [i.external_id for i in items]
    assert "111" not in ids  # promoted filtered
    assert "222" in ids and "333" in ids

    i1 = next(i for i in items if i.external_id == "222")
    assert i1.source == "olx"
    assert i1.title == "Organic One"
    assert i1.price_value == 2009
    assert "2 009" in i1.price and "zł" in i1.price
    assert i1.url == "https://www.olx.pl/d/oferta/organic1-CID99-IDaaa.html"
    assert i1.image_url and i1.image_url.startswith("https://ireland.apollo.olxcdn.com")
    assert ";s=512x512" in i1.image_url  # upscaled
    assert i1.location == "Poznań, Jeżyce"
    assert i1.published_timestamp is not None   # "Dzisiaj o 11:02"

    i2 = next(i for i in items if i.external_id == "333")
    # placeholder SVG -> no image
    assert i2.image_url is None
    # "do negocjacji" price: value is leading digits
    assert i2.price_value == 3500
    assert "do negocjacji" in i2.price
    assert i2.location == "Katowice, Dąb"
    # Polish absolute date "03 kwietnia 2026" → valid timestamp
    assert i2.published_timestamp is not None
    print("OK: olx _extract_items filters promoted and parses organic")


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
    test_olx_source_matches()
    test_olx_is_promoted()
    test_olx_strip_search_reason()
    test_olx_absolutize()
    test_olx_tz_mapping()
    test_olx_parse_relative_date()
    test_olx_image_lazy_load_fallback()
    test_olx_extract_items_organic_vs_promoted()
    print("\nALL UNIT TESTS PASSED")
