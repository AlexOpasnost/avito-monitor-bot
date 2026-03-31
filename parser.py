import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs

from curl_cffi import requests as curl_requests

from config import config

logger = logging.getLogger(__name__)

AVITO_API_KEY = "af0deccbgcgidddjgnvljitrat3lbhpb"
AVITO_MOBILE_KEY = "af0deccbgcgidddjgnvljitntccdduijhdinfgjgfjir"

# Avito category slug -> categoryId mapping for mobile API
CATEGORY_IDS = {
    "telefony": 24, "smartfony": 24,
    "noutbuki": 4, "kompyutery": 31,
    "planshety_i_elektronnye_knigi": 137,
    "audio_i_video": 32, "foto_i_videokamery": 34,
    "igry_pristavki_i_programmy": 97,
    "bytovaya_tehnika": 21, "elektronika": 6,
    "odezhda_obuv_aksessuary": 27,
    "muzhskaya_odezhda": 108, "zhenskaya_odezhda": 107,
    "detskaya_odezhda_i_obuv": 109,
    "kvartiry": 18, "komnaty": 19,
    "doma_dachi_kottedzhi": 20,
    "nedvizhimost": 4,
    "avtomobili": 9, "mototsikly_i_mototehnika": 14,
    "gruzoviki_i_spetstehnika": 81,
    "zapchasti_i_aksessuary": 10,
    "transport": 1,
    "vakansii": 110, "rezyume": 111, "rabota": 2,
    "mebel_i_interer": 35,
    "sport_i_otdyh": 39, "hobbi_i_otdyh": 7,
    "zhivotnye": 47, "sobaki": 48, "koshki": 49,
    "dlya_doma_i_dachi": 5,
    "remont_i_stroitelstvo": 112,
    "uslugi": 8, "predlozheniya_uslug": 113,
    "lichnye_veschi": 3, "krasota_i_zdorove": 110,
    "rasteniya": 106,
}

# City slug -> locationId mapping
LOCATION_IDS = {
    "all": 621540,  # Вся Россия
    "moskva": 637640, "sankt-peterburg": 653240,
    "novosibirsk": 648070, "ekaterinburg": 643720,
    "kazan": 645100, "nizhniy_novgorod": 647540,
    "chelyabinsk": 655120, "samara": 652500,
    "omsk": 649100, "rostov-na-donu": 651920,
    "ufa": 654360, "krasnoyarsk": 646200,
    "voronezh": 642440, "perm": 649900,
    "volgograd": 641940, "krasnodar": 645880,
    "saratov": 653000, "tyumen": 654080,
    "barnaul": 641400, "vladivostok": 641780,
    "irkutsk": 644640, "habarovsk": 654520,
    "yaroslavl": 656060, "tomsk": 654000,
    "orenburg": 649200, "kaliningrad": 645020,
    "tula": 654160, "ryazan": 652320,
}


@dataclass
class AvitoItem:
    avito_id: str
    title: str
    price: str
    url: str
    image_url: str | None = None
    location: str | None = None
    seller_name: str | None = None
    seller_rating: str | None = None
    seller_reviews: str | None = None
    seller_url: str | None = None
    views: str | None = None
    favorites: str | None = None
    description: str | None = None
    published_date: str | None = None


def _get_proxy() -> str | None:
    if not config.proxy_list:
        return None
    return random.choice(config.proxy_list)


def _extract_search_params(url: str) -> tuple[str, dict]:
    """Extract API URL and params. Uses web API with path, falls back to mobile API with categoryId."""
    import re as _re
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    path_parts = [p for p in parsed.path.strip("/").split("/") if p]

    # Extract city and category slugs (lowercase only)
    city_slug = None
    category_slug = None
    for part in path_parts:
        if _re.search(r'[A-Z]', part):
            continue
        if _re.match(r'^.+_\d{6,}$', part):
            continue
        if city_slug is None:
            city_slug = part
        elif category_slug is None:
            category_slug = part

    # Web API — categoryId/locationId don't work as params
    # We'll filter results client-side by category.id
    params = {
        "key": AVITO_API_KEY,
        "sort": "date",
        "display": "list",
        "limit": "50",  # Fetch more to have enough after filtering
        "page": "1",
    }

    # Pass query params from URL
    for key, values in qs.items():
        if key not in params:
            params[key] = values[0]

    if "s" not in params:
        params["s"] = "104"

    # Resolve category ID for client-side filtering
    target_category_id = None
    if category_slug:
        target_category_id = CATEGORY_IDS.get(category_slug)

    api_url = "https://www.avito.ru/web/1/main/items"

    logger.info("Parsed URL -> city=%s, cat=%s (filter_id=%s)",
        city_slug, category_slug, target_category_id)

    return api_url, params, target_category_id


async def parse_listings(url: str) -> list[AvitoItem] | None:
    """Fetch listings from Avito internal API."""
    proxy = _get_proxy()
    api_path, params, target_category_id = _extract_search_params(url)

    delay = random.uniform(config.request_delay_min, config.request_delay_max)
    await asyncio.sleep(delay)

    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host == "m.avito.ru":
        referer = url.replace("m.avito.ru", "www.avito.ru")
    elif host == "avito.ru":
        referer = url.replace("avito.ru", "www.avito.ru", 1)
    else:
        referer = url

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: _fetch_api(api_path, referer, params, proxy, target_category_id))
    except Exception as e:
        logger.error("Parse error for %s: %s", url, e)
        return None


def _fetch_api(api_url: str, referer: str, params: dict, proxy: str | None, target_category_id: int | None = None) -> list[AvitoItem] | None:
    try:
        logger.info("Fetching API: %s?categoryId=%s&locationId=%s",
            api_url[:60], params.get("categoryId", "?"), params.get("locationId", "?"))
        resp = curl_requests.get(
            api_url,
            params=params,
            impersonate="chrome",
            proxy=proxy,
            headers={
                "Referer": referer,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
            timeout=20,
        )

        if resp.status_code == 429:
            logger.warning("API 429 (rate limited)")
            return None
        if resp.status_code == 403:
            logger.warning("API 403 (blocked)")
            return None
        if resp.status_code != 200:
            logger.warning("API status: %d", resp.status_code)
            return None

        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            return None

        items_data = data.get("items", [])
        # Mobile API may wrap in "result"
        if not items_data and "result" in data:
            result = data["result"]
            if isinstance(result, dict):
                items_data = result.get("items", [])
        if not items_data:
            return []

        items = []
        for item in items_data:
            if not isinstance(item, dict):
                continue
            # Mobile API may nest item data in "value" key
            if "value" in item and isinstance(item["value"], dict):
                item = item["value"]

            avito_id = str(item.get("id", item.get("itemId", "")))
            if not avito_id:
                continue

            title = item.get("title", item.get("name", "Без названия"))

            # Filter by category
            item_cat = item.get("category", {})
            item_cat_id = item_cat.get("id", 0) if isinstance(item_cat, dict) else 0
            if target_category_id:
                if item_cat_id != target_category_id:
                    continue

            # Price
            price_info = item.get("priceDetailed", {})
            if isinstance(price_info, dict):
                price_str = price_info.get("string", "")
                price_val = price_info.get("value", 0)
                price = price_str if price_str else (f"{int(price_val):,} ₽".replace(",", " ") if price_val else "Цена не указана")
            else:
                price = "Цена не указана"

            # URL
            url_path = item.get("urlPath", "")
            if url_path and "?" in url_path:
                url_path = url_path.split("?")[0]
            item_url = f"https://www.avito.ru{url_path}" if url_path else ""

            # Image — prefer small for fast Telegram delivery
            images = item.get("images", [])
            image_url = None
            if images and isinstance(images[0], dict):
                image_url = (
                    images[0].get("278x278")
                    or images[0].get("339x339")
                    or images[0].get("636x476")
                    or images[0].get("140x140")
                )

            # Location — from API or from URL path
            loc = item.get("location", "")
            if isinstance(loc, dict):
                location = loc.get("name", "") or loc.get("formattedAddress", "")
            elif isinstance(loc, str):
                location = loc
            else:
                location = ""

            # Fallback: extract city from urlPath (e.g. /moskva/category/...)
            if not location and url_path:
                import re as _re
                city_match = _re.match(r"/([a-z_-]+)/", url_path)
                if city_match:
                    city_slug = city_match.group(1).replace("-", "_")
                    # Simple transliteration map for common cities
                    _cities = {
                        "moskva": "Москва", "sankt_peterburg": "Санкт-Петербург",
                        "novosibirsk": "Новосибирск", "ekaterinburg": "Екатеринбург",
                        "kazan": "Казань", "nizhniy_novgorod": "Нижний Новгород",
                        "chelyabinsk": "Челябинск", "samara": "Самара", "omsk": "Омск",
                        "rostov_na_donu": "Ростов-на-Дону", "ufa": "Уфа",
                        "krasnoyarsk": "Красноярск", "voronezh": "Воронеж",
                        "perm": "Пермь", "volgograd": "Волгоград",
                        "krasnodar": "Краснодар", "saratov": "Саратов",
                        "tyumen": "Тюмень", "barnaul": "Барнаул",
                        "vladivostok": "Владивосток", "irkutsk": "Иркутск",
                        "habarovsk": "Хабаровск", "yaroslavl": "Ярославль",
                        "tomsk": "Томск", "orenburg": "Оренбург",
                        "kaliningrad": "Калининград", "tula": "Тула",
                        "ryazan": "Рязань", "kirov": "Киров",
                        "simferopol": "Симферополь", "sevastopol": "Севастополь",
                        "nizhnekamsk": "Нижнекамск", "podolsk": "Подольск",
                        "blagoveshchensk": "Благовещенск", "tambov": "Тамбов",
                    }
                    location = _cities.get(city_slug, city_slug.replace("_", " ").title())

            # Description fallback
            description = item.get("imagesAlt", "")

            items.append(AvitoItem(
                avito_id=avito_id,
                title=title,
                price=price,
                url=item_url,
                image_url=image_url,
                location=location or None,
                description=description if description and description.lower() != title.lower() else None,
            ))

        # Log category distribution for debugging
        if target_category_id:
            cat_ids = {}
            for it in items_data:
                v = it.get("value", it) if isinstance(it, dict) else {}
                c = v.get("category", it.get("category", {})) if isinstance(v, dict) else {}
                cid = c.get("id", 0) if isinstance(c, dict) else 0
                cat_ids[cid] = cat_ids.get(cid, 0) + 1
            logger.info("Category distribution in API response: %s (want=%d)", dict(list(cat_ids.items())[:10]), target_category_id)

        logger.info("API returned %d items (filter cat=%s)", len(items), target_category_id)
        return items

    except Exception as e:
        logger.error("API request failed: %s", e)
        return None


def enrich_item(item: AvitoItem, proxy: str | None) -> AvitoItem:
    """Enrich item with description, seller, date from mobile API v19."""
    try:
        # Try direct API call without visiting main page first
        resp = curl_requests.get(
            f"https://m.avito.ru/api/19/items/{item.avito_id}",
            params={"key": AVITO_MOBILE_KEY},
            impersonate="chrome",
            proxy=proxy,
            headers={
                "user-agent": "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 Chrome/124.0.0.0 Mobile Safari/537.36",
                "accept": "application/json",
                "accept-language": "ru-RU,ru;q=0.9",
                "referer": "https://m.avito.ru/",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
            timeout=10,
            allow_redirects=False,
        )

        if resp.status_code != 200:
            logger.info("Enrich API returned %d for %s", resp.status_code, item.avito_id)
            return item

        d = resp.json()
        logger.info("Enrich OK for %s, keys: %d", item.avito_id, len(d))

        # Description
        desc = d.get("description", "")
        if isinstance(desc, dict):
            desc = desc.get("text", desc.get("value", ""))
        if desc and isinstance(desc, str):
            desc = re.sub(r"<[^>]+>", "", desc).strip()
            if len(desc) > 200:
                desc = desc[:200] + "..."
            item.description = desc

        # Address
        addr = d.get("address", "")
        if addr and isinstance(addr, str):
            item.location = addr

        # Seller
        seller = d.get("seller", {})
        if isinstance(seller, dict):
            item.seller_name = seller.get("name", seller.get("title", "")) or None
            rating = seller.get("rating", {})
            if isinstance(rating, dict):
                score = rating.get("score", rating.get("value", ""))
                count = rating.get("count", rating.get("reviews", ""))
                if score:
                    try:
                        item.seller_rating = f"{float(score):.1f}"
                    except (ValueError, TypeError):
                        item.seller_rating = str(score)
                if count:
                    item.seller_reviews = str(count)

        # Views
        for vk in ["viewsCount", "views", "totalViews"]:
            v = d.get(vk)
            if v and v != 0:
                if isinstance(v, dict):
                    total = v.get("total", v.get("all", 0))
                    today = v.get("today", 0)
                    item.views = f"{total} (+{today})" if today else str(total)
                else:
                    item.views = str(v)
                break

        # Favorites
        for fk in ["favoritesCount", "favorites"]:
            f = d.get(fk)
            if f and f != 0:
                if isinstance(f, dict):
                    f = f.get("total", f.get("all", 0))
                if f:
                    item.favorites = str(f)
                break

        # Time
        time_val = d.get("time", d.get("createdAt", d.get("sortTimeStamp", "")))
        if isinstance(time_val, (int, float)) and time_val > 1000000000:
            from datetime import datetime, timezone
            try:
                item.published_date = datetime.fromtimestamp(time_val, tz=timezone.utc).strftime("%H:%M:%S %d.%m.%Y")
            except (ValueError, OSError):
                pass
        elif isinstance(time_val, str) and time_val:
            item.published_date = time_val

        return item

    except Exception as e:
        logger.debug("Enrich failed for %s: %s", item.avito_id, e)
        return item


# ---------------------------------------------------------------------------
# Playwright browser singleton for enrichment
# ---------------------------------------------------------------------------

_pw_browser = None
_pw_playwright = None
_pw_lock = asyncio.Lock()


def _parse_proxy_for_playwright(proxy_str: str) -> dict:
    """Convert 'http://user:pass@host:port' to Playwright proxy dict."""
    from urllib.parse import urlparse as _urlparse
    p = _urlparse(proxy_str)
    result = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        result["username"] = p.username
    if p.password:
        result["password"] = p.password
    return result


async def _get_browser():
    """Return a shared Playwright browser instance (singleton)."""
    global _pw_browser, _pw_playwright
    async with _pw_lock:
        if _pw_browser and _pw_browser.is_connected():
            return _pw_browser

        from playwright.async_api import async_playwright
        _pw_playwright = await async_playwright().start()

        _pw_browser = await _pw_playwright.chromium.launch(headless=True)
        logger.info("Playwright browser launched")
        return _pw_browser


async def close_playwright():
    """Shutdown the shared browser (call on bot stop)."""
    global _pw_browser, _pw_playwright
    async with _pw_lock:
        if _pw_browser:
            await _pw_browser.close()
            _pw_browser = None
        if _pw_playwright:
            await _pw_playwright.stop()
            _pw_playwright = None


async def enrich_item_playwright(item: AvitoItem) -> AvitoItem:
    """Enrich item with details from Avito page via Playwright."""
    if not item.url:
        return item

    try:
        browser = await _get_browser()

        # Create context with proxy (so each page goes through mobile proxy)
        context_kwargs = {}
        if config.proxy_list:
            context_kwargs["proxy"] = _parse_proxy_for_playwright(config.proxy_list[0])
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()
        page.set_default_timeout(5000)

        try:
            await page.goto(item.url, wait_until="domcontentloaded", timeout=15000)

            # Check for captcha / block page
            title = await page.title()
            content = await page.content()
            content_lower = content.lower()
            if "captcha" in content_lower or "доступ ограничен" in content_lower or "проблема с ip" in content_lower:
                logger.info("Captcha/block for %s (title: %s)", item.avito_id, title)
                return item
            logger.info("Playwright loaded %s (title: %s, size: %d)", item.avito_id, title[:50], len(content))

            # Description
            try:
                el = await page.wait_for_selector('[data-marker="item-view/item-description"]', timeout=5000)
                if el:
                    text = (await el.text_content() or "").strip()
                    if text:
                        if len(text) > 300:
                            text = text[:300] + "..."
                        item.description = text
            except Exception:
                pass

            # Views
            try:
                el = await page.query_selector('[data-marker="item-view/total-views"]')
                if el:
                    text = (await el.text_content() or "").strip()
                    if text:
                        item.views = text
            except Exception:
                pass

            # Date
            try:
                el = await page.query_selector('[data-marker="item-view/item-date"]')
                if el:
                    text = (await el.text_content() or "").strip()
                    if text:
                        item.published_date = text
            except Exception:
                pass

            # Seller name
            try:
                el = await page.query_selector('[data-marker="seller-info/label"]')
                if el:
                    text = (await el.text_content() or "").strip()
                    if text:
                        item.seller_name = text
            except Exception:
                pass

            # Seller rating (text with "отзыв" near seller-info)
            try:
                els = await page.query_selector_all('[data-marker^="seller-info"]')
                for sel_el in els:
                    text = (await sel_el.text_content() or "").strip()
                    if "отзыв" in text.lower() or "рейтинг" in text.lower():
                        # Extract rating number like "4.8" or "4.8 · 123 отзыва"
                        m = re.search(r"(\d+[.,]\d+)", text)
                        if m:
                            item.seller_rating = m.group(1).replace(",", ".")
                        break
            except Exception:
                pass

            # Address (more precise than API)
            try:
                el = await page.query_selector('[data-marker="item-view/item-address"]')
                if el:
                    text = (await el.text_content() or "").strip()
                    if text:
                        item.location = text
            except Exception:
                pass

        finally:
            await page.close()
            await context.close()

    except Exception as e:
        logger.debug("Playwright enrich failed for %s: %s", item.avito_id, e)

    return item


async def fetch_page_title(url: str, proxy: str | None) -> str | None:
    """Fetch page title from Avito search page for subscription info."""
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: _fetch_title(url, proxy))
    except Exception:
        return None


def _fetch_title(url: str, proxy: str | None) -> str | None:
    """Get the search page title (e.g. 'Женская одежда')."""
    try:
        resp = curl_requests.get(
            url, impersonate="chrome", proxy=proxy,
            headers={"Accept-Language": "ru-RU,ru;q=0.9"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        # Extract <title> or <h1>
        m = re.search(r"<h1[^>]*>([^<]+)</h1>", resp.text)
        if m:
            title = m.group(1).strip()
            # Remove count like "2 111 986"
            title = re.sub(r"\s*[\d\s]{4,}$", "", title).strip()
            return title if title else None
        return None
    except Exception:
        return None
