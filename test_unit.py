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
    _extract_filters as _mer_extract_filters,
    _extract_keyword as _mer_extract_keyword,
    _format_jpy_price as _mer_format_price,
    _parse_item as _mer_parse_item,
)
from parsers.vinted import (
    VintedSource,
    _BREADCRUMB_RE as _vinted_breadcrumb_re,
    _apply_enrichment as _vinted_apply_enrichment,
    _build_api_url as _vinted_build_api_url,
    _extract_description_from_html as _vinted_extract_description,
    _extract_image as _vinted_extract_image,
    _extract_location_from_html as _vinted_extract_location,
    _extract_target_catalogs as _vinted_extract_target_catalogs,
    _extract_timestamp as _vinted_extract_ts,
    _parse_item as _vinted_parse_item,
    _parse_price as _vinted_parse_price,
    _parse_response as _vinted_parse_response,
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


def test_dispatcher_matches_vinted():
    s = detect_source("https://www.vinted.com/catalog?search_text=iphone")
    assert s is not None and s.name == "vinted"
    s = detect_source("https://www.vinted.fr/catalog?brand_ids[]=53")
    assert s is not None and s.name == "vinted"
    s = detect_source("https://www.vinted.co.uk/catalog?search_text=nike")
    assert s is not None and s.name == "vinted"
    print("OK: dispatcher matches vinted")


def test_dispatcher_matches_mercari():
    # JP-only host: the mercapi pipeline searches JP categories. US
    # Mercari (mercari.com) is intentionally NOT matched — accepting
    # it silently produced wrong / empty results because keywords
    # don't map across the two storefronts.
    s = detect_source("https://jp.mercari.com/search?keyword=iphone")
    assert s is not None and s.name == "mercari"
    s = detect_source("https://www.mercari.com/search?q=camera")
    assert s is None
    print("OK: dispatcher matches mercari (JP only)")


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


def test_dispatcher_rejects_ssrf_payloads():
    # Substring-based regexes were vulnerable: a `?u=https://avito.ru/x`
    # query in any URL would match. The hostname-allowlist fix makes
    # detect_source operate on urlparse().hostname only, so these all
    # resolve to attacker-controlled hosts and must be rejected.
    ssrf_cases = [
        # Internal/loopback targets pretending to host a marketplace URL
        "http://127.0.0.1/admin?u=https://avito.ru/x",
        "http://localhost:6379/?https://kufar.by/x",
        "http://[::1]/?u=https://olx.pl/x",
        # AWS metadata endpoint
        "http://169.254.169.254/latest/meta-data/?u=https://vinted.fr/x",
        # Public attacker domain with a marketplace URL hidden in query
        "http://attacker.com/redirect?url=https://avito.ru/x",
        "https://evil.example/?olx.pl=1",
        # Confusable subdomain — hostname is attacker.com, not avito.ru
        "https://www.avito.ru.attacker.com/x",
        "https://kufar.by.evil.tld/x",
        # Userinfo trick — netloc is attacker.com
        "http://avito.ru@attacker.com/admin",
        # Non-marketplace TLD that the old regex's `[a-z]{2,3}` would have
        # admitted via substring (e.g. olx.zip in a query) — now rejected
        # because hostname is attacker.com
        "http://attacker.com/?u=https://olx.zip/x",
    ]
    for url in ssrf_cases:
        assert detect_source(url) is None, f"SSRF leak: {url!r} matched a source"
    print(f"OK: dispatcher rejects {len(ssrf_cases)} SSRF payloads")


def test_supported_sources():
    sources = supported_sources()
    assert "avito" in sources
    assert "kufar" in sources
    assert "olx" in sources
    assert "mercari" in sources
    assert "vinted" in sources
    assert "youla" in sources
    assert "fruitsfamily" in sources
    assert "grailed" in sources
    print(f"OK: supported sources = {sources}")


def test_grailed_url_routing():
    from parsers.grailed import GrailedSource, _algolia_params_from_url
    s = GrailedSource()
    assert s.matches("https://www.grailed.com/categories/menswear/tops")
    assert s.matches("https://grailed.com/shop?query=stone+island")
    assert not s.matches("https://avito.ru/")
    # SSRF reject — hostname is attacker.com
    assert not s.matches("http://attacker.com/?u=https://grailed.com/x")
    # /shop?query=...
    p = _algolia_params_from_url("https://www.grailed.com/shop?query=stone+island")
    assert "query=stone%20island" in p or "query=stone+island" in p, p
    assert "hitsPerPage=40" in p
    # /categories/menswear/tops
    p = _algolia_params_from_url("https://www.grailed.com/categories/menswear/tops")
    assert "department%3Amenswear" in p, p
    assert "category%3Atops" in p, p
    # /designers/stone-island — strict facet filter, not optional boost
    p = _algolia_params_from_url("https://www.grailed.com/designers/stone-island")
    assert "designers.name%3Astone%20island" in p, p
    # Should be in facetFilters (strict), not optionalFacetFilters (boost)
    assert "facetFilters=" in p and "optionalFacetFilters=" not in p, p
    # Personal feed → refuse
    assert _algolia_params_from_url("https://www.grailed.com/feed/Z2dDpGWAdg") is None
    # Login / homepage with no query → refuse
    assert _algolia_params_from_url("https://www.grailed.com/") is None
    print("OK: GrailedSource.matches + _algolia_params_from_url")


def test_youla_url_parsing():
    from parsers.youla import _parse_user_url
    # Path-based URL with city slug + category — extract category as keyword
    kw, pf, pt = _parse_user_url("https://youla.ru/sankt-peterburg/iphone-13")
    assert kw == "iphone 13", f"got {kw!r}"
    assert pf is None and pt is None
    # Post-redirect form with q= and price filter
    kw, pf, pt = _parse_user_url(
        "https://youla.ru/sankt-peterburg?q=iphone+13"
        "&attributes%5Bprice%5D%5Bfrom%5D=1000"
        "&attributes%5Bprice%5D%5Bto%5D=5000",
    )
    assert "iphone" in (kw or "").lower(), f"got {kw!r}"
    assert pf == 1000 and pt == 5000
    # No keyword extractable → None (refuse to fan out to whole site)
    kw, pf, pt = _parse_user_url("https://youla.ru/")
    assert kw is None
    # Listing URL with trailing 24-hex object id should not pollute keyword
    kw, _, _ = _parse_user_url(
        "https://youla.ru/sankt-peterburg/odezhda 5d03af8393800097504b8282",
    )
    # Will go through path-segment branch; we don't assert specific output
    # — just that it doesn't crash.
    print("OK: youla _parse_user_url")


def test_youla_source_matches():
    from parsers.youla import YoulaSource
    s = YoulaSource()
    assert s.matches("https://youla.ru/all/odezhda")
    assert s.matches("https://m.youla.ru/all")
    assert not s.matches("https://avito.ru/")
    # SSRF: hostname is attacker.com, query has youla.ru — must reject
    assert not s.matches("http://attacker.com/?u=https://youla.ru/x")
    assert not s.matches("https://youla.ru.attacker.com/x")
    print("OK: YoulaSource.matches (incl. SSRF rejection)")


def test_fruitsfamily_source_matches():
    from parsers.fruitsfamily import FruitsfamilySource, _filter_from_url
    s = FruitsfamilySource()
    assert s.matches("https://fruitsfamily.com/search?gender=MEN&subcategoryIds=1")
    assert s.matches("https://www.fruitsfamily.com/")
    assert not s.matches("https://avito.ru/")
    assert not s.matches("http://attacker.com/?u=https://fruitsfamily.com/x")
    # Filter extraction — single subcategory
    f = _filter_from_url(
        "https://fruitsfamily.com/search?gender=MEN&subcategoryIds=1",
    )
    assert f == {"gender": "MEN", "subcategory_ids": [1]}, f"got {f!r}"
    # Filter extraction — multi subcategory + price
    f = _filter_from_url(
        "https://fruitsfamily.com/search?gender=WOMEN"
        "&subcategoryIds=1&subcategoryIds=2&priceMin=10000&priceMax=50000",
    )
    assert f["gender"] == "WOMEN"
    assert f["subcategory_ids"] == [1, 2]
    assert f["price_min"] == 10000 and f["price_max"] == 50000
    # No filter at all → None (refuse fan-out)
    assert _filter_from_url("https://fruitsfamily.com/") is None
    print("OK: FruitsfamilySource.matches + _filter_from_url")


# ---------- avito plugin internals ----------

def test_avito_source_matches():
    src = AvitoSource()
    assert src.matches("https://www.avito.ru/x")
    assert src.matches("https://m.avito.ru/x")
    assert src.matches("https://avito.ru/")
    assert not src.matches("https://kufar.by/x")
    # SSRF payloads — must reject:
    assert not src.matches("http://127.0.0.1/?u=https://avito.ru/")
    assert not src.matches("https://www.avito.ru.attacker.com/x")
    assert not src.matches("http://avito.ru@attacker.com/x")
    assert not src.matches("https://attacker.com/?fake=avito.ru/x")
    print("OK: AvitoSource.matches (incl. SSRF rejection)")


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


def test_vinted_source_matches():
    src = VintedSource()
    assert src.matches("https://www.vinted.com/catalog?search_text=iphone")
    assert src.matches("https://www.vinted.fr/catalog?brand_ids[]=53")
    assert src.matches("https://m.vinted.de/")
    assert src.matches("https://www.vinted.co.uk/catalog?search_text=nike")
    assert not src.matches("https://www.avito.ru/x")
    assert not src.matches("https://example.com/")
    assert not src.matches("")
    print("OK: VintedSource.matches")


def test_vinted_build_api_url():
    # Plain search text → API URL with order=newest_first injected
    api = _vinted_build_api_url(
        "https://www.vinted.com/catalog?search_text=iphone"
    )
    assert api is not None
    assert "/api/v2/catalog/items?" in api
    assert "search_text=iphone" in api
    assert "order=newest_first" in api
    assert "page=1" in api and "per_page=50" in api

    # User's URL with order/page/per_page already → all overwritten
    api = _vinted_build_api_url(
        "https://www.vinted.com/catalog?search_text=nike"
        "&order=price_high_to_low&page=5&per_page=200"
    )
    assert api is not None
    assert "order=newest_first" in api  # forced newest
    assert "page=1" in api and "per_page=50" in api
    assert "order=price_high_to_low" not in api
    # No double per_page= ever
    assert api.count("per_page=") == 1

    # Brand-only filter (no keyword) is still a valid search
    api = _vinted_build_api_url(
        "https://www.vinted.fr/catalog?brand_ids%5B%5D=53"
    )
    assert api is not None
    # parse_qs decodes %5B%5D back to [], so the API param is brand_ids[]
    assert "brand_ids" in api

    # Price-range filter alone counts
    api = _vinted_build_api_url(
        "https://www.vinted.com/catalog?price_from=10&price_to=50&currency=EUR"
    )
    assert api is not None and "price_from=10" in api

    # Empty / no filter — reject (would flood with whole catalogue)
    assert _vinted_build_api_url("https://www.vinted.com/catalog") is None
    assert _vinted_build_api_url("https://www.vinted.com/") is None
    assert _vinted_build_api_url(
        "https://www.vinted.com/catalog?order=newest_first"
    ) is None
    print("OK: vinted _build_api_url")


def test_vinted_parse_price():
    # Standard Vinted shape — returns (label, value, currency)
    label, value, cur = _vinted_parse_price({"amount": "115.0", "currency_code": "USD"})
    assert value == 115 and cur == "USD"
    assert "$" in label and "115" in label

    # EUR — native render, no hardcoded USD hint anymore (user-currency
    # estimate is appended at render time by parsers.currency).
    label, value, cur = _vinted_parse_price({"amount": "100.0", "currency_code": "EUR"})
    assert value == 100 and cur == "EUR" and "€" in label and "$" not in label

    # Unknown currency: keep code as-is
    label, value, cur = _vinted_parse_price({"amount": "50", "currency_code": "XXX"})
    assert value == 50 and cur == "XXX" and "XXX" in label and "$" not in label

    # Zero / negative / missing
    assert _vinted_parse_price({"amount": "0", "currency_code": "USD"}) == (
        "Цена не указана", None, None,
    )
    assert _vinted_parse_price({}) == ("Цена не указана", None, None)
    assert _vinted_parse_price(None) == ("Цена не указана", None, None)
    print("OK: vinted _parse_price")


def test_vinted_extract_image_and_ts():
    entry = {
        "photos": [
            {
                "url": "https://images1.vinted.net/t/abc/f800/1777116722.jpeg",
                "high_resolution": {"timestamp": 1777116722},
            },
            {"url": "https://images1.vinted.net/t/def/f800/x.jpeg"},
        ],
        "photo": {
            "url": "https://images1.vinted.net/t/abc/f800/1777116722.jpeg",
            "high_resolution": {"timestamp": 1777116722},
        },
    }
    assert _vinted_extract_image(entry) == (
        "https://images1.vinted.net/t/abc/f800/1777116722.jpeg"
    )
    assert _vinted_extract_ts(entry) == 1777116722

    # Empty / missing
    assert _vinted_extract_image({}) is None
    assert _vinted_extract_ts({}) is None
    # Non-http URL rejected
    assert _vinted_extract_image({"photos": [{"url": "//cdn/x.jpg"}]}) is None
    print("OK: vinted _extract_image / _extract_timestamp")


def test_vinted_parse_item_full_shape():
    entry = {
        "id": 8743309093,
        "title": "Iphone 15 plus",
        "price": {"amount": "115.0", "currency_code": "USD"},
        "is_visible": True,
        "brand_title": "Apple",
        "size_title": "",
        "status": "Very good",
        "path": "/items/8743309093-iphone-15-plus",
        "url": "https://www.vinted.com/items/8743309093-iphone-15-plus",
        "promoted": False,
        "user": {"id": 1, "login": "dashal15"},
        "photos": [{
            "url": "https://images1.vinted.net/t/abc/f800/1777116722.jpeg",
            "high_resolution": {"timestamp": 1777116722},
        }],
    }
    item = _vinted_parse_item(entry)
    assert item.source == "vinted"
    assert item.external_id == "8743309093"
    # Brand kept on its own field, not in title (Google Translate
    # was mangling brands: "Under Armour" → "Под броню").
    assert item.brand == "Apple"
    # Title is the seller's verbatim title — no condition/size folded
    # in. Renderer appends them post-translation in the user's language.
    assert item.title == "Iphone 15 plus"
    assert item.condition == "Very good"
    assert item.size is None  # size_title was empty
    assert item.currency == "USD"
    assert item.price_value == 115
    assert "$" in item.price
    assert item.url == "https://www.vinted.com/items/8743309093-iphone-15-plus"
    assert item.image_url and item.image_url.startswith("https://images1.vinted.net/")
    assert item.seller_name == "dashal15"
    assert item.published_timestamp == 1777116722
    assert item.location is None and item.description is None
    print("OK: vinted _parse_item full shape (brand on its own field)")


def test_vinted_extract_target_catalogs():
    # Single catalog id
    cats = _vinted_extract_target_catalogs(
        "https://www.vinted.es/catalog?catalog[]=2050"
    )
    assert cats == frozenset({2050})

    # Multiple catalog[] occurrences
    cats = _vinted_extract_target_catalogs(
        "https://www.vinted.es/catalog?catalog[]=2050&catalog[]=77"
    )
    assert cats == frozenset({2050, 77})

    # CSV form
    cats = _vinted_extract_target_catalogs(
        "https://www.vinted.es/catalog?catalog_ids=5,77,2050"
    )
    assert cats == frozenset({5, 77, 2050})

    # No catalog filter -> empty (skip strict filter)
    cats = _vinted_extract_target_catalogs(
        "https://www.vinted.es/catalog?search_text=iphone&brand_ids[]=14"
    )
    assert cats == frozenset()
    print("OK: vinted _extract_target_catalogs")


def test_vinted_breadcrumb_regex():
    """Real breadcrumb HTML structure from a vinted.es item page."""
    html = (
        '<ul class="breadcrumbs"><li><a href="/catalog/5-men?referrer=item-crumbs">'
        'Hombre</a></li><li><a href="/catalog/2050-clothing?referrer=item-crumbs">'
        'Ropa</a></li><li><a href="/catalog/30-activewear?referrer=item-crumbs">'
        'Ropa deportiva</a></li><li><a href="/catalog/582-tracksuits?referrer=item-crumbs">'
        'Tracksuits</a></li></ul>'
    )
    ids = [int(m) for m in _vinted_breadcrumb_re.findall(html)]
    assert ids == [5, 2050, 30, 582]

    # Multi-digit, hyphen-with-numbers slug
    html2 = (
        '<a href="/catalog/4690593-some-niche-brand?referrer=item-crumbs">x</a>'
    )
    ids = [int(m) for m in _vinted_breadcrumb_re.findall(html2)]
    assert ids == [4690593]

    # Wrong referrer (catalog page itself, not item) -> no match
    html3 = '<a href="/catalog/5-men?referrer=catalog">x</a>'
    assert _vinted_breadcrumb_re.findall(html3) == []
    print("OK: vinted _BREADCRUMB_RE")


def test_vinted_extract_description_from_html():
    # JSON-LD path — Vinted's primary description carrier
    html = '''
<!DOCTYPE html><html><head>
<title>x</title>
<script type="application/ld+json">{"@type":"Product","name":"Casaco Adidas Italia","description":"Casaco da adidas com detalhe da bandeira d Italia.\\nUsado poucas vezes\\nPreco negociavel","image":"https://...","brand":{"@type":"Brand","name":"adidas"}}</script>
</head><body></body></html>
'''
    desc = _vinted_extract_description(html)
    assert desc is not None
    assert "Casaco da adidas" in desc and "Preco negociavel" in desc

    # og:description fallback (no JSON-LD) — title prefix gets stripped
    html2 = (
        '<meta property="og:description" '
        'content="Casaco Adidas Italia - Casaco da adidas usado poucas vezes."'
        '/>'
    )
    desc = _vinted_extract_description(html2)
    assert desc == "Casaco da adidas usado poucas vezes."

    # Nothing parseable -> None
    assert _vinted_extract_description("<html><body>nope</body></html>") is None
    print("OK: vinted _extract_description_from_html")


def test_vinted_extract_location_from_html():
    # The user_info block in Vinted's React stream comes through as
    # backslash-escaped JSON inside a JS-string. The bytes really are
    # `\"text\":\"...\",\"key\":\"location\"`.
    html_chunk = (
        r'\"icon\":\"LocationPin16\",'
        r'\"text\":\"Vila Nova de Gaia, Portugal\",\"key\":\"location\"'
    )
    loc = _vinted_extract_location(html_chunk)
    assert loc == "Vila Nova de Gaia, Portugal"

    # Unicode escape inside the value (e.g. Málaga)
    html_chunk2 = (
        r'\"text\":\"Málaga, España\",\"key\":\"location\"'
    )
    loc = _vinted_extract_location(html_chunk2)
    # unicode_escape decodes á to á, ñ to ñ
    assert loc == "Málaga, España"

    # No location -> None
    assert _vinted_extract_location("<html>no user_info</html>") is None
    print("OK: vinted _extract_location_from_html")


def test_vinted_apply_enrichment():
    from parsers.base import SearchItem
    item = SearchItem(
        source="vinted", external_id="1", title="x", price="10 €",
        price_value=10, url="x", image_url=None, location=None,
        description=None, seller_name=None, published_timestamp=None,
    )
    _vinted_apply_enrichment(item, {
        "ancestors": frozenset({5, 2050}),
        "description": "Bonita prenda",
        "location": "Madrid, España",
    })
    assert item.description == "Bonita prenda"
    assert item.location == "Madrid, España"

    # Existing fields are preserved (don't overwrite)
    item2 = SearchItem(
        source="vinted", external_id="2", title="y", price="20 €",
        price_value=20, url="y", image_url=None,
        location="Already Set", description="Already Set",
        seller_name=None, published_timestamp=None,
    )
    _vinted_apply_enrichment(item2, {
        "description": "From Vinted",
        "location": "From Vinted",
    })
    assert item2.description == "Already Set"
    assert item2.location == "Already Set"

    # Empty / missing meta keys don't blow up
    item3 = SearchItem(
        source="vinted", external_id="3", title="z", price="30 €",
        price_value=30, url="z", image_url=None, location=None,
        description=None, seller_name=None, published_timestamp=None,
    )
    _vinted_apply_enrichment(item3, {})
    _vinted_apply_enrichment(item3, {"description": "  "})  # whitespace
    assert item3.description is None and item3.location is None
    print("OK: vinted _apply_enrichment")


def test_vinted_parse_response_filters_promoted():
    data = {
        "items": [
            {  # promoted → skip
                "id": 1, "title": "Promoted", "promoted": True,
                "price": {"amount": "10", "currency_code": "USD"},
            },
            {  # is_visible False → skip
                "id": 2, "title": "Hidden",
                "is_visible": False,
                "price": {"amount": "20", "currency_code": "USD"},
            },
            {  # organic
                "id": 3, "title": "Organic Phone",
                "is_visible": True, "promoted": False,
                "brand_title": "Apple",
                "price": {"amount": "100.0", "currency_code": "EUR"},
                "url": "https://www.vinted.com/items/3-organic",
                "photos": [{
                    "url": "https://images1.vinted.net/t/x/f800/123.jpeg",
                    "high_resolution": {"timestamp": 1777116722},
                }],
                "user": {"login": "alice"},
            },
        ],
        "pagination": {"total_entries": 3},
    }
    out = _vinted_parse_response(data)
    assert out is not None
    ids = [i.external_id for i in out]
    assert ids == ["3"]
    assert out[0].seller_name == "alice"
    print("OK: vinted _parse_response filters promoted + hidden")


def test_mercari_source_matches():
    src = MercariSource()
    assert src.matches("https://jp.mercari.com/search?keyword=iphone")
    assert src.matches("https://www.mercari.jp/")
    # US Mercari (mercari.com) is intentionally rejected — see
    # test_dispatcher_matches_mercari for rationale.
    assert not src.matches("https://www.mercari.com/")
    assert not src.matches("https://www.avito.ru/x")
    assert not src.matches("")
    print("OK: MercariSource.matches (JP only)")


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


def test_mercari_extract_filters():
    # Plain keyword URL → keyword filter only
    f = _mer_extract_filters("https://jp.mercari.com/search?keyword=iphone")
    assert f is not None
    assert f["keyword"] == "iphone"
    assert f["categories"] == [] and f["brands"] == []

    # User's real URL: category_id + brand_id CSV
    f = _mer_extract_filters(
        "https://jp.mercari.com/search?category_id=2&brand_id=1242%2C24%2C17736"
    )
    assert f is not None
    assert f["keyword"] == ""
    assert f["categories"] == [2]
    assert f["brands"] == [1242, 24, 17736]

    # keyword + category + price range + condition
    f = _mer_extract_filters(
        "https://jp.mercari.com/search?keyword=macbook"
        "&category_id=719&price_min=30000&price_max=150000"
        "&item_condition_id=1,2"
    )
    assert f is not None
    assert f["keyword"] == "macbook"
    assert f["categories"] == [719]
    assert f["price_min"] == 30000 and f["price_max"] == 150000
    assert f["item_conditions"] == [1, 2]

    # Nothing to search for → None
    assert _mer_extract_filters("https://jp.mercari.com/search") is None
    assert _mer_extract_filters("https://jp.mercari.com/search?sort=created_time") is None

    # Dedup (user URL rarely has this but our parser should be safe)
    f = _mer_extract_filters("https://jp.mercari.com/search?brand_id=24,24,17736")
    assert f["brands"] == [24, 17736]
    print("OK: _mer_extract_filters")


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
    # Native price only — user-currency estimate is appended at render
    # time by parsers.currency.format_with_estimate.
    assert "2 500 zł" in item.price and "$" not in item.price
    assert item.price_value == 2500
    assert item.currency == "PLN"
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


# ---------- currency conversion ----------

def test_currency_convert_and_format():
    from parsers.currency import convert, format_with_estimate, format_native

    # Same currency — pass-through
    assert convert(100, "USD", "USD") == 100.0
    assert convert(100, "rub", "RUB") == 100.0  # case-insensitive

    # USD pivot (rough — rates can drift)
    eur_to_rub = convert(1, "EUR", "RUB")
    assert eur_to_rub and 60 < eur_to_rub < 200

    # Unknown currency → None
    assert convert(100, "EUR", "XXX") is None
    assert convert(100, "XXX", "EUR") is None
    assert convert(None, "EUR", "RUB") is None

    # Native render — symbols, no thousands sep for small ints
    assert format_native(14, "EUR").replace(" ", "") in ("14€", "14EUR")
    assert format_native(None, "EUR", fallback="—") == "—"

    # With estimate — same currency suppresses parens
    same = format_with_estimate(14, "EUR", "EUR")
    assert "(~" not in same and "€" in same
    # Different currency adds parens
    diff = format_with_estimate(14, "EUR", "RUB")
    assert "(~" in diff and "₽" in diff

    print("OK: currency convert + format")


# ---------- description prettify ----------

def test_prettify_description_sentence_end():
    from scheduler import _prettify_description

    # Original screenshot bug: line break after "Originally:" was
    # winning over the sentence period after "не носил.", leaving an
    # awkward "Первоначально:..." dangling.
    raw = (
        "Размер 48 - Jack &amp; Черные костюмные брюки Jones Premium "
        "(JPRFRANCO), размер 48. Состояние хорошее, без следов и пятен. "
        "Никогда не носил.\n"
        "Первоначально: 80€"
    )
    out = _prettify_description(raw)
    # &amp; should be decoded
    assert "&amp;" not in out and "Jack & " in out
    # Cut at the period after "не носил.", drop the "Первоначально" trailer
    assert "не носил." in out
    assert "Первоначально" not in out
    # No naked "..." at the end either
    assert "..." not in out

    # Long description with no sentence end — should fall back to word
    # boundary with «…»
    raw2 = "x " * 200  # all whitespace-separated single chars, no periods
    out2 = _prettify_description(raw2)
    assert out2.endswith("…")

    # Empty input
    assert _prettify_description("") == ""
    assert _prettify_description(None) == ""

    # Bottom-fluff strip — "договорная" trailer goes
    raw3 = "Хорошие штаны.\nдоговорная"
    assert "договорная" not in _prettify_description(raw3)

    print("OK: scheduler _prettify_description")


def test_format_notification_includes_condition_and_user_currency():
    from scheduler import _format_notification
    from parsers.base import SearchItem

    item = SearchItem(
        source="vinted", external_id="1",
        title="Pantalones Jack & Jones",
        price="14 €", price_value=14, url="https://x", image_url=None,
        location="Madrid, España",
        description="Buen estado.",
        seller_name="polatic", published_timestamp=None,
        condition="Nuevo sin etiquetas", size="M", currency="EUR",
    )
    text = _format_notification(
        item,
        title="Брюки Jack & Jones",
        description="В хорошем состоянии.",
        condition="Новый с биркой",
        user_currency="RUB",
    )
    # Title carries (size, condition) — both translated where possible
    assert "Брюки Jack &amp; Jones" in text  # html-escaped
    assert "M" in text and "Новый с биркой" in text
    # Native price + RUB estimate
    assert "14 €" in text and "₽" in text and "(~" in text
    # Location is rendered
    assert "Madrid" in text
    print("OK: scheduler _format_notification (size+condition, native+RUB)")


# ---------- bot_i18n integrity ----------

def test_bot_i18n_lists_consistent():
    from bot_i18n import (
        LANGUAGES, LANGUAGE_CODES, CURRENCIES, CURRENCY_CODES,
        TIMEZONES, TIMEZONE_CODES,
        DEFAULT_TZ_FOR_LANG, default_tz_for_lang,
        language_keyboard, currency_keyboard, timezone_keyboard,
        main_menu_keyboard,
    )
    from parsers.currency import USD_RATES

    # No duplicate codes
    assert len(LANGUAGES) == len(LANGUAGE_CODES)
    assert len(CURRENCIES) == len(CURRENCY_CODES)
    assert len(TIMEZONES) == len(TIMEZONE_CODES)

    # Every selectable currency must have a USD rate so format_with_estimate
    # can convert into it.
    for code, _ in CURRENCIES:
        assert code.upper() in USD_RATES, f"missing rate for {code}"

    # Every language with an explicit default-tz mapping must point at a
    # timezone that's in the picker (otherwise the user opens the picker
    # and sees their default not selected).
    for lang_code, tz in DEFAULT_TZ_FOR_LANG.items():
        assert tz in TIMEZONE_CODES, f"default tz {tz} for {lang_code} missing from picker"

    # default_tz_for_lang fallback for unknown / missing lang
    assert default_tz_for_lang(None) == "Europe/Moscow"
    assert default_tz_for_lang("xx") == "Europe/Moscow"
    assert default_tz_for_lang("ru") == "Europe/Moscow"
    assert default_tz_for_lang("es") == "Europe/Madrid"

    # All keyboards build without crashing and have at least one row
    assert language_keyboard().inline_keyboard
    assert currency_keyboard().inline_keyboard
    assert timezone_keyboard().inline_keyboard
    assert main_menu_keyboard().inline_keyboard

    # back_callback wires through the picker
    kb = timezone_keyboard("menu:profile")
    last_row = kb.inline_keyboard[-1]
    assert last_row[0].callback_data == "menu:profile"

    print("OK: bot_i18n list integrity")


def test_format_when_local():
    """Render timestamps in the user's TZ + language with the right
    phrasing per locale."""
    from datetime import datetime, timezone
    from scheduler import _format_when_local

    # Empty timestamp → dash
    assert _format_when_local(None) == "—"
    assert _format_when_local(0) == "—"

    # A fixed timestamp: 2026-04-26 12:00:00 UTC = 15:00 МСК = 14:00 CEST.
    ts = int(datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc).timestamp())
    moscow_ru = _format_when_local(ts, "Europe/Moscow", "ru")
    madrid_ru = _format_when_local(ts, "Europe/Madrid", "ru")
    assert "15:00" in moscow_ru and "МСК" in moscow_ru
    assert "14:00" in madrid_ru and "Мадрид" in madrid_ru

    # English phrasing — "Today at" / "Yesterday at" + English city tag
    # (Cyrillic "Мадрид" is reserved for ru/be/uk/bg/kk users now.)
    madrid_en = _format_when_local(ts, "Europe/Madrid", "en")
    assert "14:00" in madrid_en and "Madrid" in madrid_en
    assert "Мадрид" not in madrid_en, "EN users should see latin city names"
    assert madrid_en.startswith(("Today at", "Yesterday at")) or "at " in madrid_en

    # Spanish phrasing — "Hoy a las" / "Ayer a las"
    madrid_es = _format_when_local(ts, "Europe/Madrid", "es")
    assert "14:00" in madrid_es
    assert "Hoy" in madrid_es or "Ayer" in madrid_es or "a las" in madrid_es

    # Polish — "Dziś o" / "Wczoraj o"
    warsaw_pl = _format_when_local(ts, "Europe/Warsaw", "pl")
    assert ("Dziś" in warsaw_pl or "Wczoraj" in warsaw_pl
            or "o " in warsaw_pl)

    # Unknown language falls back to RU phrasing
    moscow_xx = _format_when_local(ts, "Europe/Moscow", "xx")
    assert "Сегодня" in moscow_xx or "Вчера" in moscow_xx or " в " in moscow_xx

    # Bad TZ falls back to UTC, doesn't crash
    bad = _format_when_local(ts, "Mars/Olympus", "en")
    assert "12:00" in bad and "UTC" in bad

    print("OK: scheduler _format_when_local")


def test_tariff_state_resolution():
    """_resolve_tariff_state collapses a raw users-row into the
    render-ready snapshot used by /profile, /tariffs, and the add-sub
    paywall. Covers: never-activated, active, expired, legacy."""
    from datetime import datetime, timezone, timedelta
    from handlers import _resolve_tariff_state

    now = datetime.now(timezone.utc)

    # Never activated → no access
    s = _resolve_tariff_state({"tariff": None, "expires_at": None, "trial_used": False})
    assert s["active"] is False and s["max_subs"] == 0
    assert s["tariff_id"] is None and s["trial_used"] is False

    # Active Basic, 10 days left
    s = _resolve_tariff_state({
        "tariff": "basic",
        "expires_at": now + timedelta(days=10, minutes=5),
        "trial_used": True,
    })
    assert s["active"] is True
    assert s["tariff_id"] == "basic"
    assert s["max_subs"] == 1
    assert 9 <= s["days_left"] <= 10  # rounding tolerance

    # Active Pro = 5 searches
    s = _resolve_tariff_state({
        "tariff": "pro",
        "expires_at": now + timedelta(days=20),
        "trial_used": True,
    })
    assert s["active"] is True and s["max_subs"] == 5

    # Expired tariff → free (no access), but trial_used carries over
    # so the user can't claim Trial again.
    s = _resolve_tariff_state({
        "tariff": "pro",
        "expires_at": now - timedelta(hours=1),
        "trial_used": True,
    })
    assert s["active"] is False and s["max_subs"] == 0
    assert s["trial_used"] is True

    # Legacy = grandfathered, no expiry, max 5
    s = _resolve_tariff_state({
        "tariff": "legacy",
        "expires_at": None,
        "trial_used": False,
    })
    assert s["active"] is True
    assert s["tariff_id"] == "legacy"
    assert s["max_subs"] == 5
    assert s["days_left"] is None  # no countdown for legacy

    print("OK: handlers _resolve_tariff_state")


def test_admin_ids_env_parsing():
    """ADMIN_IDS=csv has priority; ADMIN_ID single is back-compat;
    both set → union (no dups)."""
    import os
    from importlib import reload
    import config as cfg_mod

    saved = (os.environ.pop("ADMIN_IDS", None), os.environ.pop("ADMIN_ID", None))
    try:
        # Plain CSV
        os.environ["ADMIN_IDS"] = " 100, 200 ,300 "
        reload(cfg_mod)
        assert sorted(cfg_mod.config.admin_ids) == [100, 200, 300]

        # Back-compat: only ADMIN_ID
        os.environ.pop("ADMIN_IDS", None)
        os.environ["ADMIN_ID"] = "777"
        reload(cfg_mod)
        assert cfg_mod.config.admin_ids == [777]

        # Both → union, no duplicates
        os.environ["ADMIN_IDS"] = "111,222"
        os.environ["ADMIN_ID"] = "222"  # already in list
        reload(cfg_mod)
        assert sorted(cfg_mod.config.admin_ids) == [111, 222]

        # Empty / garbage tolerated
        os.environ["ADMIN_IDS"] = ", , abc, 555 ,"
        os.environ.pop("ADMIN_ID", None)
        reload(cfg_mod)
        assert cfg_mod.config.admin_ids == [555]

        # Nothing set → empty
        os.environ.pop("ADMIN_IDS", None)
        reload(cfg_mod)
        assert cfg_mod.config.admin_ids == []
    finally:
        # Restore
        for key, val in zip(("ADMIN_IDS", "ADMIN_ID"), saved):
            os.environ.pop(key, None)
            if val is not None:
                os.environ[key] = val
        reload(cfg_mod)
        # Also reload handlers since it imports config
        import handlers as handlers_mod
        reload(handlers_mod)

    print("OK: config admin_ids parsing")


def test_html_injection_safe_in_sub_list():
    """Sub names + URLs are user-controlled and must be HTML-escaped
    before going into a parse_mode='HTML' message body. A malicious
    name containing <b> or <a> would otherwise corrupt the layout
    (and pave the way for XSS if the admin panel ever moves to web)."""
    import html

    # Direct sanity check on the helper we use everywhere
    assert html.escape("<script>alert(1)</script>") == \
        "&lt;script&gt;alert(1)&lt;/script&gt;"
    # Quote-escape for href contexts (we pass quote=True)
    assert html.escape('"&\'', quote=True) == "&quot;&amp;&#x27;"

    # _sub_display_name doesn't escape — escaping is the renderer's job.
    # Confirm the rendered HTML in handlers.py matches: `<a href="{safe_url}">{safe_name}</a>`
    from handlers import _sub_display_name
    raw = {"name": "<b>Pwn</b>", "source": "vinted"}
    assert _sub_display_name(raw) == "<b>Pwn</b>"  # raw value preserved
    # The handler MUST escape before rendering, but we don't render
    # here in tests. The integration check is by code review +
    # manual test in Telegram (visible if escaping is missing).
    print("OK: html escape primitives behave")


def test_admin_user_tariff_short_circuit():
    """is_admin telegram_id → synthetic admin tariff (max_subs=999),
    no DB lookup, no expiry. Removing from list = instant demote."""
    import asyncio
    from importlib import reload
    import os
    import config as cfg_mod

    saved = os.environ.pop("ADMIN_IDS", None)
    try:
        os.environ["ADMIN_IDS"] = "12345"
        reload(cfg_mod)
        import handlers as handlers_mod
        reload(handlers_mod)

        # is_admin happy/sad path
        assert handlers_mod.is_admin(12345) is True
        assert handlers_mod.is_admin(99999) is False
        assert handlers_mod.is_admin(None) is False

        # Synthetic state for admin (no DB hit needed because
        # short-circuit returns before db.get_user_tariff is called).
        # We pass user_id=-1 (would fail in DB if it got that far).
        state = asyncio.run(handlers_mod._user_tariff_state(-1, telegram_id=12345))
        assert state["active"] is True
        assert state["tariff_id"] == "admin"
        assert state["max_subs"] == 999
        assert state["trial_used"] is True  # trial button hidden
    finally:
        os.environ.pop("ADMIN_IDS", None)
        if saved is not None:
            os.environ["ADMIN_IDS"] = saved
        reload(cfg_mod)
        import handlers as handlers_mod
        reload(handlers_mod)

    print("OK: handlers admin tariff short-circuit")


def test_disabled_sources_kill_switch():
    """DISABLED_SOURCES env var → is_source_disabled flips to True
    on listed names. Empty / missing → all sources allowed.

    Used by:
      - scheduler.py: skip cycle (no error increment) for paused source
      - handlers.handle_url: reject new subscription with friendly msg
    """
    import os
    from importlib import reload
    import config as cfg_mod

    saved = os.environ.pop("DISABLED_SOURCES", None)
    try:
        os.environ["DISABLED_SOURCES"] = " AVITO , kufar ,, "
        reload(cfg_mod)
        # parsers/__init__.py reads config lazily so no need to reload it
        from parsers import is_source_disabled

        # Case-insensitive match + whitespace tolerated
        assert is_source_disabled("avito") is True
        assert is_source_disabled("AVITO") is True
        assert is_source_disabled("kufar") is True
        # Not in list
        assert is_source_disabled("olx") is False
        assert is_source_disabled("vinted") is False
        # Edge cases
        assert is_source_disabled("") is False
        assert is_source_disabled(None) is False

        # Empty env var → no source disabled
        os.environ["DISABLED_SOURCES"] = ""
        reload(cfg_mod)
        assert is_source_disabled("avito") is False
    finally:
        os.environ.pop("DISABLED_SOURCES", None)
        if saved is not None:
            os.environ["DISABLED_SOURCES"] = saved
        reload(cfg_mod)
    print("OK: parsers is_source_disabled kill-switch")


def test_legal_footer_renders_only_published_links():
    """Onboarding hero appends «соглашаешься с …» whenever PRIVACY/OFFER
    URLs are set. After May 2026 the dataclass defaults point at
    GitHub Pages, so the footer ALWAYS renders in the current build —
    a 152-ФЗ Art. 9 informed-consent fix. We test override behaviour
    here, not "empty env = no footer" (that's no longer the case).
    """
    import os
    from importlib import reload
    import config as cfg_mod

    saved_p = os.environ.pop("PRIVACY_URL", None)
    saved_o = os.environ.pop("OFFER_URL", None)
    try:
        # No env override → defaults take over → footer renders.
        reload(cfg_mod)
        import handlers as handlers_mod
        reload(handlers_mod)
        out = handlers_mod._legal_footer()
        assert "офертой" in out and "политикой" in out, (
            "default-built footer must mention both legal docs; got " + repr(out)
        )

        # Only offer override
        os.environ["OFFER_URL"] = "https://example.com/offer"
        reload(cfg_mod)
        reload(handlers_mod)
        out = handlers_mod._legal_footer()
        assert "https://example.com/offer" in out
        # privacy still falls through to the GitHub Pages default
        assert "alexopasnost.github.io" in out

        # Both overridden
        os.environ["PRIVACY_URL"] = "https://example.com/privacy"
        reload(cfg_mod)
        reload(handlers_mod)
        out = handlers_mod._legal_footer()
        assert "офертой" in out and "политикой" in out
        assert "https://example.com/privacy" in out
        assert "https://example.com/offer" in out
    finally:
        for k in ("PRIVACY_URL", "OFFER_URL"):
            os.environ.pop(k, None)
        if saved_p is not None:
            os.environ["PRIVACY_URL"] = saved_p
        if saved_o is not None:
            os.environ["OFFER_URL"] = saved_o
        reload(cfg_mod)
        import handlers as handlers_mod
        reload(handlers_mod)
    print("OK: handlers _legal_footer publishes only configured links")


def test_yookassa_receipt_item_shape():
    """build_receipt_item produces the exact dict shape YooKassa
    expects in `provider_data.receipt.items[]` for самозанятый.

    Critical detail: the receipt amount.value is in RUBLES (string,
    2 decimals) — NOT kopeks like the Telegram-Payments LabeledPrice.
    Mixing these up would either charge the user 100x or 1/100x of
    the expected price.
    """
    from services.yookassa import build_receipt_item

    item = build_receipt_item("Базовый", 1290.0)
    assert item["description"] == "Базовый"
    assert item["amount"]["value"] == "1290.00"
    assert item["amount"]["currency"] == "RUB"
    assert item["quantity"] == "1.00"
    # vat_code=1 = «Без НДС» — required for НПД (самозанятый)
    assert item["vat_code"] == 1
    assert item["payment_mode"] == "full_payment"
    assert item["payment_subject"] == "service"

    # Long names are truncated to FFD 1.2 max (128)
    long_item = build_receipt_item("X" * 200, 100.0)
    assert len(long_item["description"]) == 128

    # Decimal handling — kopeks-precise prices render correctly
    item2 = build_receipt_item("Pro", 2990.50)
    assert item2["amount"]["value"] == "2990.50"
    print("OK: yookassa build_receipt_item shape + RUB units")


def test_tariff_rules_consistency():
    """Each tariff that appears in the user-facing _TARIFFS list must
    have matching rules in _TARIFF_RULES (max_subs/hours/kopeks),
    otherwise the buy-callback would crash on a known tariff."""
    from handlers import _TARIFFS, _TARIFF_RULES

    for tid, name, *_ in _TARIFFS:
        assert tid in _TARIFF_RULES, f"_TARIFFS has {tid!r} but _TARIFF_RULES doesn't"

    # Trial must be free, others must have a positive price
    assert _TARIFF_RULES["trial"]["kopeks"] == 0
    for tid in ("basic", "advanced", "pro"):
        assert _TARIFF_RULES[tid]["kopeks"] > 0

    # Pro must allow more searches than Basic
    assert _TARIFF_RULES["pro"]["max_subs"] > _TARIFF_RULES["basic"]["max_subs"]

    print("OK: handlers _TARIFFS ↔ _TARIFF_RULES consistency")


def test_mercari_enrich_items():
    """Enrichment: pull location / description / seller_name from a
    full-item lookup, cache for 24h, never lose a previously-set field."""
    import asyncio
    from parsers.base import SearchItem
    from parsers.mercari import _enrich_items, _ENRICH_CACHE

    _ENRICH_CACHE.clear()

    class FakeArea:
        def __init__(self, name): self.name = name
    class FakeSeller:
        def __init__(self, name): self.name = name
    class FakeFullItem:
        def __init__(self, area, desc, seller):
            self.shipping_from_area = FakeArea(area) if area else None
            self.description = desc
            self.seller = FakeSeller(seller) if seller else None

    full_items = {
        "m1": FakeFullItem("東京都", "Mint condition", "tanaka"),
        "m2": FakeFullItem("Osaka",  "",                "yamada"),
        # m3 has nothing useful
        "m3": FakeFullItem("",       "",                ""),
    }

    fetch_count = {"n": 0}

    class FakeMercapi:
        async def item(self, id_):
            fetch_count["n"] += 1
            return full_items[id_]

    items = [
        SearchItem(source="mercari", external_id="m1", title="t1",
                   price="1000 ¥", price_value=1000, url="https://x/1",
                   image_url=None, location=None, description=None,
                   seller_name=None, published_timestamp=None),
        SearchItem(source="mercari", external_id="m2", title="t2",
                   price="2000 ¥", price_value=2000, url="https://x/2",
                   image_url=None, location=None, description=None,
                   seller_name=None, published_timestamp=None),
        SearchItem(source="mercari", external_id="m3", title="t3",
                   price="3000 ¥", price_value=3000, url="https://x/3",
                   image_url=None, location=None, description=None,
                   seller_name=None, published_timestamp=None),
    ]

    asyncio.run(_enrich_items(items, FakeMercapi()))

    assert items[0].location == "東京都"
    assert items[0].description == "Mint condition"
    assert items[0].seller_name == "tanaka"

    assert items[1].location == "Osaka"
    assert items[1].description is None  # empty desc stays None
    assert items[1].seller_name == "yamada"

    # Empty everything — fields stay None
    assert items[2].location is None
    assert items[2].description is None
    assert items[2].seller_name is None

    assert fetch_count["n"] == 3, "should fetch each id once on first run"

    # Second run hits the cache — no new fetches
    fetch_count["n"] = 0
    items2 = [
        SearchItem(source="mercari", external_id="m1", title="t1-bis",
                   price="1000 ¥", price_value=1000, url="https://x/1",
                   image_url=None, location=None, description=None,
                   seller_name=None, published_timestamp=None),
    ]
    asyncio.run(_enrich_items(items2, FakeMercapi()))
    assert fetch_count["n"] == 0, "cached id_ shouldn't refetch"
    assert items2[0].location == "東京都"

    print("OK: mercari _enrich_items (location/description/seller + cache)")


def test_sub_display_name():
    """Custom user-set name wins; otherwise we render the marketplace
    pretty name (Авито/Mercari/Vinted/…). Empty/whitespace counts as
    «no name set»."""
    from handlers import _sub_display_name

    # Custom name set
    assert _sub_display_name({"name": "Брюки 48", "source": "vinted"}) == "Брюки 48"
    # Whitespace-only treated as unset
    assert _sub_display_name({"name": "   ", "source": "vinted"}) == "Vinted"
    # No name → source pretty name
    assert _sub_display_name({"name": None, "source": "mercari"}) == "Mercari"
    assert _sub_display_name({"name": None, "source": "avito"}) == "Авито"
    # Missing source falls back to "маркетплейс"
    assert _sub_display_name({"name": None}) == "маркетплейс"

    print("OK: handlers _sub_display_name")


def test_vinted_location_fallback():
    """When user_info doesn't carry the location entry, fall back to
    stitching `city` + `country_title_local`."""
    from parsers.vinted import _extract_location_from_html as _loc

    # Primary path still wins when present
    primary = (
        '<html>...{ a:b }... \\"text\\":\\"Paris, Francia\\",'
        '\\"key\\":\\"location\\" ...</html>'
    )
    assert _loc(primary) == "Paris, Francia"

    # Fallback: only city + country_title_local exposed
    fallback_both = (
        '<html>{"some":"thing"}\\"city\\":\\"Madrid\\"...'
        '\\"country_title_local\\":\\"España\\"...</html>'
    )
    assert _loc(fallback_both) == "Madrid, España"

    # City alone (no country)
    fallback_city = '<html>...\\"city\\":\\"Berlin\\"...</html>'
    assert _loc(fallback_city) == "Berlin"

    # Country alone (no city)
    fallback_country = '<html>...\\"country_title_local\\":\\"Italia\\"...</html>'
    assert _loc(fallback_country) == "Italia"

    # Nothing at all → None
    assert _loc("<html>no location here</html>") is None

    print("OK: vinted _extract_location_from_html (with fallback)")


def test_blacklist_normalize():
    from database import Database
    n = Database._normalize_blacklist
    # Comma + newline + semicolon separators
    assert n("женский, детский\nфейк; мусор") == [
        "женский", "детский", "фейк", "мусор",
    ]
    # Lowercased, dedup, ordered
    assert n(["WOMAN", "  woman  ", "kid"]) == ["woman", "kid"]
    # Length bounds (2..30)
    assert n("a") == []
    assert n("x" * 31) == []
    # Empty / None tolerated
    assert n("") == []
    assert n(None) == []
    # 50-word cap
    big = n(",".join(f"word{i}" for i in range(60)))
    assert len(big) == 50
    print("OK: Database._normalize_blacklist")


def test_blacklist_filter():
    from scheduler import _matches_blacklist, _parse_blacklist
    from parser import SearchItem

    item = SearchItem(
        source="avito", external_id="1",
        title="Женский свитер Stone Island",
        price="", price_value=None, url="", image_url=None,
        location=None, description="Размер M, новый",
        seller_name=None, published_timestamp=None,
    )
    # Substring match catches all declensions
    assert _matches_blacklist(item, ["женск"]) is True
    assert _matches_blacklist(item, ["мужск"]) is False
    # Multiple words — any match wins
    assert _matches_blacklist(item, ["мужск", "женск"]) is True
    # Empty list matches nothing
    assert _matches_blacklist(item, []) is False
    # Case-insensitive (blacklist is normalized to lowercase before this)
    assert _matches_blacklist(item, ["stone island"]) is True

    # JSONB normalization — asyncpg may surface JSONB as either str or list
    assert _parse_blacklist(None) == []
    assert _parse_blacklist('["женск","детск"]') == ["женск", "детск"]
    assert _parse_blacklist(["a", "b"]) == ["a", "b"]
    assert _parse_blacklist("not json") == []
    assert _parse_blacklist([1, "ok", None, "good"]) == ["ok", "good"]
    print("OK: scheduler blacklist filter + parser")


if __name__ == "__main__":
    test_proxy_for_source()
    test_dispatcher_matches_avito()
    test_dispatcher_matches_kufar()
    test_dispatcher_matches_olx()
    test_dispatcher_rejects_unknown()
    test_dispatcher_rejects_ssrf_payloads()
    test_supported_sources()
    test_youla_url_parsing()
    test_youla_source_matches()
    test_fruitsfamily_source_matches()
    test_grailed_url_routing()
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
    test_dispatcher_matches_vinted()
    test_vinted_source_matches()
    test_vinted_build_api_url()
    test_vinted_parse_price()
    test_vinted_extract_image_and_ts()
    test_vinted_parse_item_full_shape()
    test_vinted_extract_target_catalogs()
    test_vinted_breadcrumb_regex()
    test_vinted_extract_description_from_html()
    test_vinted_extract_location_from_html()
    test_vinted_apply_enrichment()
    test_vinted_parse_response_filters_promoted()
    test_dispatcher_matches_mercari()
    test_mercari_source_matches()
    test_mercari_extract_keyword()
    test_mercari_extract_filters()
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
    test_currency_convert_and_format()
    test_prettify_description_sentence_end()
    test_format_notification_includes_condition_and_user_currency()
    test_bot_i18n_lists_consistent()
    test_format_when_local()
    test_vinted_location_fallback()
    test_sub_display_name()
    test_mercari_enrich_items()
    test_tariff_state_resolution()
    test_tariff_rules_consistency()
    test_yookassa_receipt_item_shape()
    test_disabled_sources_kill_switch()
    test_legal_footer_renders_only_published_links()
    test_admin_ids_env_parsing()
    test_admin_user_tariff_short_circuit()
    test_html_injection_safe_in_sub_list()
    test_blacklist_normalize()
    test_blacklist_filter()
    print("\nALL UNIT TESTS PASSED")
