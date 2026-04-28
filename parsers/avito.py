"""Avito marketplace parser — cloudscraper + HTML hydration JSON."""
from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
from urllib.parse import unquote

import orjson

from config import config
from .base import SearchItem
from .common import (
    MAX_JSON_BYTES,
    download_image_bytes,
    get_cloudscraper,
    global_request_lock,
    host_in_allowlist,
    invalidate_session,
    looks_like_image_url,
    proxies_dict,
    rotate_ip,
)

logger = logging.getLogger(__name__)

_HOST = "avito"
_WARMUP_URLS = ("https://www.avito.ru/", "https://m.avito.ru/")

# Strict hostname allowlist. Was previously `re.search(r"avito\.ru/", url)`
# which matched substrings anywhere in the URL (incl. the query string),
# letting an attacker SSRF arbitrary hosts via `?u=https://avito.ru/x`.
# urlparse().hostname extraction + exact set membership closes that path.
_AVITO_HOSTS = frozenset({"avito.ru", "www.avito.ru", "m.avito.ru"})


class AvitoSource:
    name = "avito"

    def matches(self, url: str) -> bool:
        return host_in_allowlist(url, _AVITO_HOSTS)

    async def fetch(
        self, url: str, proxy: str | None, max_retries: int = 3,
    ) -> list[SearchItem] | None:
        async with global_request_lock():
            try:
                return await _fetch_inner(url, proxy, max_retries)
            finally:
                import random
                cooldown = random.uniform(5.0, 10.0)
                logger.info("[avito] post-request cooldown %.1fs (lock held)", cooldown)
                await asyncio.sleep(cooldown)


async def _fetch_inner(url: str, proxy: str | None, max_retries: int) -> list[SearchItem] | None:
    for attempt in range(max_retries):
        items, blocked = await _fetch_hydration_json(url, proxy)
        if items is not None:
            logger.info("[avito] fetched %d items for %s", len(items), url[:80])
            return items
        if not blocked:
            return None
        logger.warning(
            "[avito] blocked (attempt %d/%d), rotating IP and retrying",
            attempt + 1, max_retries,
        )
        invalidate_session(_HOST)
        await rotate_ip()
        await asyncio.sleep(8)
    logger.error("[avito] all %d attempts blocked for %s", max_retries, url[:80])
    return None


async def _fetch_hydration_json(url: str, proxy: str | None):
    loop = asyncio.get_running_loop()
    resp_data = await loop.run_in_executor(None, lambda: _fetch_html_sync(url, proxy))
    if resp_data is None:
        return None, False
    status, html, resp_headers = resp_data

    if status in (429, 403):
        logger.warning("[avito] BLOCKED %d for %s — headers: %s", status, url[:80], resp_headers)
        return None, True
    if status in (301, 302, 303, 307, 308):
        logger.warning("[avito] REDIRECT %d (block) for %s", status, url[:80])
        return None, True
    if status != 200:
        logger.debug("[avito] HTTP %d for %s", status, url[:80])
        return None, False
    if "проблема с ip" in html.lower() or "доступ ограничен" in html.lower():
        logger.warning("[avito] BLOCKED (IP problem page) for %s", url[:80])
        return None, True
    logger.info("[avito] page loaded: %d, size=%d", status, len(html))

    items = _extract_catalog_items_strict(html, url)
    if items is None:
        logger.warning("[avito] no catalog MFE on page (size=%d)", len(html))
        return None, False
    logger.info("[avito] parsed %d catalog items (strict)", len(items))
    return items, False


def _fetch_html_sync(url: str, proxy: str | None):
    try:
        s = get_cloudscraper(_HOST, warmup_urls=list(_WARMUP_URLS), proxy=proxy)
        proxies = proxies_dict(proxy)
        logger.info("[avito] REQUEST url=%r (len=%d)", url, len(url))
        resp = s.get(url, proxies=proxies, timeout=60, allow_redirects=False)
        logger.info("[avito] response final_url=%r, status=%d", str(resp.url), resp.status_code)
        return resp.status_code, resp.text, dict(resp.headers)
    except Exception as e:
        logger.debug("[avito] sync fetch error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Catalog extraction — strict mfe-state with state.data.catalog
# ---------------------------------------------------------------------------

def _extract_catalog_items_strict(html: str, url: str) -> list[SearchItem] | None:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("[avito] beautifulsoup4 not installed")
        return None

    soup = BeautifulSoup(html, "html.parser")
    mfe_scripts = soup.select('script[data-mfe-state="true"]')
    logger.info("[avito] found %d mfe-state scripts", len(mfe_scripts))

    # First pass — parse each script
    parsed_mfes = []
    for idx, script in enumerate(mfe_scripts):
        body = (script.text or "").strip()
        if not body or "sandbox" in body[:200]:
            continue
        if len(body) > MAX_JSON_BYTES:
            logger.warning("[avito] mfe #%d body oversized: %d bytes", idx, len(body))
            continue
        try:
            data = orjson.loads(html_lib.unescape(body))
        except Exception as e:
            logger.debug("[avito] mfe #%d JSON decode err: %s", idx, str(e)[:80])
            continue
        if not isinstance(data, dict):
            continue
        state_data = (data.get("state") or {}).get("data")
        if not isinstance(state_data, dict):
            logger.info("[avito] mfe #%d: no state.data", idx)
            continue
        logger.info("[avito] mfe #%d state.data keys: %s",
                    idx, list(state_data.keys())[:30])
        parsed_mfes.append((idx, state_data))

    filter_marker_fields = (
        "totalCount", "totalElements", "mainCount", "count",
        "searchHash", "filtersV2", "filtersGroup", "searchCore", "mcId",
    )

    candidates = []
    for idx, state_data in parsed_mfes:
        catalog = state_data.get("catalog")
        if not isinstance(catalog, dict):
            continue
        items_raw = catalog.get("items")
        if not isinstance(items_raw, list):
            continue
        has_markers = any(state_data.get(k) not in (None, "") for k in filter_marker_fields)
        logger.info(
            "[avito] mfe #%d catalog: %d items, filter-markers=%s, catalog.keys=%s",
            idx, len(items_raw), has_markers, list(catalog.keys())[:20],
        )
        candidates.append((idx, catalog, state_data, has_markers))

    if not candidates:
        logger.warning("[avito] no catalog MFE found")
        return None

    picked = next(((i, c) for i, c, _s, m in candidates if m), None)
    if picked is None:
        logger.warning("[avito] no catalog MFE has filter markers — using first candidate")
        idx, catalog = candidates[0][0], candidates[0][1]
    else:
        idx, catalog = picked
        logger.info("[avito] picked mfe #%d as the filter-applied catalog", idx)

    items_raw = catalog.get("items") or []

    items: list[SearchItem] = []
    skipped_non_item = 0
    for raw in items_raw:
        if not isinstance(raw, dict):
            continue
        rtype = (raw.get("type") or "").strip()
        if rtype and rtype not in ("item", ""):
            skipped_non_item += 1
            continue
        val = raw.get("value", raw) if "value" in raw else raw
        if not isinstance(val, dict):
            continue
        if not (val.get("id") or val.get("itemId")):
            continue
        try:
            items.append(_parse_item(val))
        except Exception as e:
            logger.debug("[avito] parse item err: %s", e)

    logger.info(
        "[avito] mfe #%d is CATALOG: %d raw rows -> %d items (%d non-item rows skipped)",
        idx, len(items_raw), len(items), skipped_non_item,
    )

    if items:
        total = len(items)
        with_image = sum(1 for i in items if i.image_url)
        with_loc = sum(1 for i in items if i.location)
        with_desc = sum(1 for i in items if i.description)
        with_ts = sum(1 for i in items if i.published_timestamp)
        logger.info(
            "[avito] completeness: image=%d/%d, location=%d/%d, desc=%d/%d, date=%d/%d",
            with_image, total, with_loc, total, with_desc, total, with_ts, total,
        )
        sample_paths = [i.url.replace("https://www.avito.ru", "")[:60] for i in items[:3]]
        logger.info("[avito] sample item paths: %s", sample_paths)
    return items


# ---------------------------------------------------------------------------
# Single-item parsing
# ---------------------------------------------------------------------------

_AVITO_IMAGE_SIZE_KEYS = (
    "864x864", "636x636", "540x540", "472x472", "432x432",
    "864x648", "636x476", "540x405", "432x324", "318x238", "208x208",
    "140x105", "72x54",
)
_AVITO_IMAGE_HOST_HINTS = ("avito.st", "avito.ru/images", "avatars.mds.yandex")


def _parse_item(val: dict) -> SearchItem:
    avito_id = str(val.get("id") or val.get("itemId") or "")
    title = val.get("title") or ""

    price_info = val.get("priceDetailed") or val.get("price") or {}
    price_value = None
    price_str = "Цена не указана"
    if isinstance(price_info, dict):
        price_value = price_info.get("value")
        price_str = price_info.get("string") or ""
        if not price_str and price_value:
            price_str = f"{int(price_value):,} ₽".replace(",", " ")
    elif isinstance(price_info, (int, float)):
        price_value = int(price_info)
        price_str = f"{price_value:,} ₽".replace(",", " ")

    url_path = val.get("urlPath") or val.get("url") or ""
    if url_path and not url_path.startswith("http"):
        item_url = f"https://www.avito.ru{url_path}"
    else:
        item_url = url_path or "https://www.avito.ru"

    image_url = _extract_image_url(val)
    location = _extract_location(val)

    desc = val.get("description") or ""
    if isinstance(desc, dict):
        desc = desc.get("text") or ""

    seller = val.get("seller") or {}
    seller_name = seller.get("name") if isinstance(seller, dict) else None

    ts_raw = (
        val.get("sortTimeStamp")
        or val.get("time")
        or val.get("publishDate")
        or val.get("sortTime")
    )
    ts: int | None = None
    if isinstance(ts_raw, (int, float)):
        ts_int = int(ts_raw)
        ts = ts_int // 1000 if ts_int > 1_000_000_000_000 else ts_int

    return SearchItem(
        source="avito",
        external_id=avito_id,
        title=title,
        price=price_str,
        price_value=int(price_value) if isinstance(price_value, (int, float)) else None,
        url=item_url,
        image_url=image_url,
        location=location,
        description=(desc.strip() if isinstance(desc, str) and desc.strip() else None),
        seller_name=seller_name,
        published_timestamp=ts,
    )


def _extract_image_url(val: dict) -> str | None:
    for key in ("images", "imagesAlt", "photos", "gallery"):
        lst = val.get(key)
        if isinstance(lst, list) and lst:
            url = _image_from_dict(lst[0])
            if url:
                return url
    for key in ("image", "cover", "mainImage", "thumbnail"):
        obj = val.get(key)
        if isinstance(obj, dict):
            url = _image_from_dict(obj)
            if url:
                return url
        elif isinstance(obj, str) and looks_like_image_url(obj, _AVITO_IMAGE_HOST_HINTS):
            return obj
    return None


def _image_from_dict(obj) -> str | None:
    if isinstance(obj, str):
        return obj if looks_like_image_url(obj, _AVITO_IMAGE_HOST_HINTS) else None
    if not isinstance(obj, dict):
        return None
    for k in _AVITO_IMAGE_SIZE_KEYS:
        v = obj.get(k)
        if looks_like_image_url(v, _AVITO_IMAGE_HOST_HINTS):
            return v
    for nest_key in ("variants", "sizes", "urls"):
        nested = obj.get(nest_key)
        if isinstance(nested, dict):
            for k in _AVITO_IMAGE_SIZE_KEYS:
                v = nested.get(k)
                if looks_like_image_url(v, _AVITO_IMAGE_HOST_HINTS):
                    return v
            for v in nested.values():
                if looks_like_image_url(v, _AVITO_IMAGE_HOST_HINTS):
                    return v
    for v in obj.values():
        if looks_like_image_url(v, _AVITO_IMAGE_HOST_HINTS):
            return v
    return None


# City slug → human name (Russian). Unknown slugs fall back to Title Case.
_CITY_SLUG_MAP = {
    "moskva": "Москва", "sankt-peterburg": "Санкт-Петербург",
    "novosibirsk": "Новосибирск", "ekaterinburg": "Екатеринбург",
    "nizhniy_novgorod": "Нижний Новгород", "kazan": "Казань",
    "chelyabinsk": "Челябинск", "omsk": "Омск", "samara": "Самара",
    "rostov-na-donu": "Ростов-на-Дону", "ufa": "Уфа",
    "krasnoyarsk": "Красноярск", "perm": "Пермь", "voronezh": "Воронеж",
    "volgograd": "Волгоград", "krasnodar": "Краснодар",
    "saratov": "Саратов", "tyumen": "Тюмень", "tolyatti": "Тольятти",
    "izhevsk": "Ижевск", "barnaul": "Барнаул", "ulyanovsk": "Ульяновск",
    "irkutsk": "Иркутск", "khabarovsk": "Хабаровск",
    "vladivostok": "Владивосток", "yaroslavl": "Ярославль",
    "makhachkala": "Махачкала", "tomsk": "Томск", "orenburg": "Оренбург",
    "kemerovo": "Кемерово", "novokuznetsk": "Новокузнецк",
    "ryazan": "Рязань", "astrakhan": "Астрахань", "penza": "Пенза",
    "naberezhnye_chelny": "Набережные Челны", "lipetsk": "Липецк",
    "kirov": "Киров", "cheboksary": "Чебоксары", "tula": "Тула",
    "kaliningrad": "Калининград", "balashikha": "Балашиха",
    "kursk": "Курск", "sevastopol": "Севастополь",
    "sochi": "Сочи", "stavropol": "Ставрополь", "ulan-ude": "Улан-Удэ",
    "tver": "Тверь", "magnitogorsk": "Магнитогорск", "ivanovo": "Иваново",
    "bryansk": "Брянск", "simferopol": "Симферополь",
    "belgorod": "Белгород", "surgut": "Сургут", "vladimir": "Владимир",
    "nizhniy_tagil": "Нижний Тагил", "arkhangelsk": "Архангельск",
    "chita": "Чита", "groznyy": "Грозный", "kaluga": "Калуга",
    "smolensk": "Смоленск", "yakutsk": "Якутск", "sterlitamak": "Стерлитамак",
    "volzhskiy": "Волжский", "saransk": "Саранск", "podolsk": "Подольск",
    "kurgan": "Курган", "cherepovets": "Череповец", "oryol": "Орёл",
    "orel": "Орёл", "vologda": "Вологда", "kostroma": "Кострома",
    "tambov": "Тамбов", "pskov": "Псков", "murmansk": "Мурманск",
    "taganrog": "Таганрог", "komsomolsk-na-amure": "Комсомольск-на-Амуре",
    "nizhnevartovsk": "Нижневартовск", "petrozavodsk": "Петрозаводск",
    "yoshkar-ola": "Йошкар-Ола", "syktyvkar": "Сыктывкар",
    "khimki": "Химки", "mytishchi": "Мытищи", "lyubertsy": "Люберцы",
    "krasnogorsk": "Красногорск", "korolev": "Королёв",
    "engels": "Энгельс", "nakhodka": "Находка", "blagoveshchensk": "Благовещенск",
    "novorossiysk": "Новороссийск", "pyatigorsk": "Пятигорск",
    "maykop": "Майкоп", "novyy_urengoy": "Новый Уренгой",
    "balakovo": "Балаково", "abakan": "Абакан",
    "armavir": "Армавир", "staryy_oskol": "Старый Оскол",
    "dzerzhinsk": "Дзержинск", "zheleznodorozhnyy": "Железнодорожный",
    "murom": "Муром", "novocherkassk": "Новочеркасск",
    "elektrostal": "Электросталь", "arzamas": "Арзамас",
    "dubna": "Дубна", "serpukhov": "Серпухов",
    "orekhovo-zuevo": "Орехово-Зуево", "ramenskoye": "Раменское",
    "schelkovo": "Щёлково", "zhukovskiy": "Жуковский",
    "noginsk": "Ногинск", "sergiev_posad": "Сергиев Посад",
    "pavlovskiy_posad": "Павловский Посад", "klin": "Клин",
    "reutov": "Реутов", "voskresensk": "Воскресенск",
    "lobnya": "Лобня", "solnechnogorsk": "Солнечногорск",
    "nizhnekamsk": "Нижнекамск", "almetyevsk": "Альметьевск",
    "zelenodolsk": "Зеленодольск",
    "kopeisk": "Копейск", "zlatoust": "Златоуст", "miass": "Миасс",
    "tobolsk": "Тобольск", "kamensk-uralskiy": "Каменск-Уральский",
    "serov": "Серов", "pervouralsk": "Первоуральск",
    "berezniki": "Березники", "solikamsk": "Соликамск",
    "votkinsk": "Воткинск", "sarapul": "Сарапул",
    "yurga": "Юрга", "belovo": "Белово",
    "leninsk-kuznetskiy": "Ленинск-Кузнецкий",
    "prokopyevsk": "Прокопьевск", "mezhdurechensk": "Междуреченск",
    "seversk": "Северск",
    "rzhev": "Ржев", "novomoskovsk": "Новомосковск",
    "angarsk": "Ангарск", "bratsk": "Братск",
    "kotlas": "Котлас", "severodvinsk": "Северодвинск",
    "velikiy_novgorod": "Великий Новгород",
    "vyborg": "Выборг", "gatchina": "Гатчина", "vsevolozhsk": "Всеволожск",
    "kovrov": "Ковров", "aleksandrov": "Александров",
    "kineshma": "Кинешма", "feodosiya": "Феодосия",
    "kerch": "Керчь", "yalta": "Ялта", "evpatoriya": "Евпатория",
    "dzhankoy": "Джанкой", "saki": "Саки", "bakhchisaray": "Бахчисарай",
    "gelendzhik": "Геленджик", "anapa": "Анапа", "tuapse": "Туапсе",
    "kropotkin": "Кропоткин", "slavyansk-na-kubani": "Славянск-на-Кубани",
    "temryuk": "Темрюк", "yeysk": "Ейск", "labinsk": "Лабинск",
    "tikhoretsk": "Тихорецк", "belorechensk": "Белореченск",
    "kurganinsk": "Курганинск", "apsheronsk": "Апшеронск",
    "chaykovskiy": "Чайковский", "krasnokamsk": "Краснокамск",
    "dimitrovgrad": "Димитровград", "syzran": "Сызрань",
    "novokuybyshevsk": "Новокуйбышевск", "chapaevsk": "Чапаевск",
    "otradnyy": "Отрадный", "kinel": "Кинель",
    "gubkinskiy": "Губкинский", "muravlenko": "Муравленко",
    "labytnangi": "Лабытнанги", "nadym": "Надым",
    "salekhard": "Салехард", "kogalym": "Когалым",
    "pyt-yakh": "Пыть-Ях", "megion": "Мегион",
    "raduzhnyy": "Радужный", "langepas": "Лангепас",
    "uray": "Урай", "yugorsk": "Югорск", "nyagan": "Нягань",
    "buynaksk": "Буйнакск", "khasavyurt": "Хасавюрт",
    "kaspiysk": "Каспийск", "derbent": "Дербент",
    "nalchik": "Нальчик", "vladikavkaz": "Владикавказ",
    "cherkessk": "Черкесск", "magadan": "Магадан",
    "yuzhno-sakhalinsk": "Южно-Сахалинск",
    "petropavlovsk-kamchatskiy": "Петропавловск-Камчатский",
    "khanty-mansiysk": "Ханты-Мансийск",
    "neryungri": "Нерюнгри", "mirnyy": "Мирный",
    "aldan": "Алдан", "lensk": "Ленск",
    "mosrentgen": "Мосрентген", "zelenograd": "Зеленоград",
    "troitsk": "Троицк",
    "odintsovo": "Одинцово", "pushkino": "Пушкино",
    "ivanteyevka": "Ивантеевка", "fryazino": "Фрязино",
    "dolgoprudnyy": "Долгопрудный", "dmitrov": "Дмитров",
    "yegoryevsk": "Егорьевск", "kolomna": "Коломна", "zaraysk": "Зарайск",
    "kashira": "Кашира", "stupino": "Ступино", "chekhov": "Чехов",
    "naro-fominsk": "Наро-Фоминск", "shatura": "Шатура",
    "obninsk": "Обнинск", "kolpino": "Колпино", "pushkin": "Пушкин",
    "petergof": "Петергоф", "lomonosov": "Ломоносов",
    "kronshtadt": "Кронштадт", "sestroretsk": "Сестрорецк",
    "zelenogorsk": "Зеленогорск",
}


def _city_from_url_path(url_path: str) -> str | None:
    if not url_path:
        return None
    parts = url_path.lstrip("/").split("/", 1)
    if not parts or not parts[0]:
        return None
    slug = parts[0].lower()
    if slug == "all":
        return None
    if slug in _CITY_SLUG_MAP:
        return _CITY_SLUG_MAP[slug]
    pretty = slug.replace("-", " ").replace("_", " ").strip()
    if pretty:
        return pretty[:1].upper() + pretty[1:]
    return None


def _extract_location(val: dict) -> str | None:
    loc = val.get("location")
    if isinstance(loc, dict):
        for k in ("name", "namePrepositional", "nameLocative",
                  "formattedAddress", "text"):
            v = loc.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    elif isinstance(loc, str) and loc.strip():
        return loc.strip()

    geo = val.get("geo")
    if isinstance(geo, dict):
        for k in ("formattedAddress", "address", "name", "text"):
            v = geo.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        refs = geo.get("geoReferences")
        if isinstance(refs, list):
            parts = [r.get("content") for r in refs
                     if isinstance(r, dict) and r.get("content")]
            if parts:
                return ", ".join(parts)

    addr = val.get("addressDetailed")
    if isinstance(addr, dict):
        for k in ("text", "name", "address", "formatted"):
            v = addr.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    elif isinstance(addr, str) and addr.strip():
        return addr.strip()

    return _city_from_url_path(val.get("urlPath") or "")


# ---------------------------------------------------------------------------
# Image download with Avito referer (for Telegram photo upload)
# ---------------------------------------------------------------------------

async def avito_download_image(url: str, proxy: str | None = None) -> bytes | None:
    return await download_image_bytes(
        url, host=_HOST, referer="https://www.avito.ru/", proxy=proxy,
    )
