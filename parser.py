import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs, unquote, urlencode

import requests as std_requests

from config import config

logger = logging.getLogger(__name__)


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
    return config.proxy_list[0]


async def rotate_ip() -> bool:
    """Call proxy provider's IP rotation endpoint. Returns True on success."""
    if not config.proxy_rotate_url:
        logger.warning("No PROXY_ROTATE_URL configured, cannot rotate IP")
        return False
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(config.proxy_rotate_url) as resp:
                if resp.status == 200:
                    logger.info("IP rotated successfully")
                    await asyncio.sleep(3)
                    return True
                else:
                    body = await resp.text()
                    logger.warning("IP rotation HTTP %d: %s", resp.status, body[:200])
                    return False
    except Exception as e:
        logger.warning("IP rotation failed: %s", e)
        return False


def _make_proxies(proxy: str | None) -> dict | None:
    """Convert proxy string to requests-compatible proxies dict."""
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


# Realistic Chrome browser headers
_CHROME_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}


async def check_proxy_ip() -> str | None:
    """Check what IP the proxy is actually using. Returns IP string or None."""
    proxy = _get_proxy()
    if not proxy:
        return None
    try:
        loop = asyncio.get_event_loop()
        def _check():
            resp = std_requests.get(
                "https://api.ipify.org?format=json",
                proxies=_make_proxies(proxy),
                timeout=15,
            )
            return resp.json().get("ip")
        ip = await loop.run_in_executor(None, _check)
        logger.info("Proxy IP: %s", ip)
        return ip
    except Exception as e:
        logger.warning("Proxy IP check failed: %s", e)
        return None


def _is_blocked(html: str, status_code: int) -> bool:
    """Check if Avito blocked the request."""
    if status_code in (403, 429):
        return True
    html_lower = html.lower() if html else ""
    return (
        "captcha" in html_lower
        or "проблема с ip" in html_lower
        or "доступ ограничен" in html_lower
        or "geetest" in html_lower
    )


def _url_to_api_params(url: str) -> dict | None:
    """Convert Avito search URL to mobile API parameters.

    Example URL: https://www.avito.ru/moskva/kvartiry/prodam-ASgBAgICAUSSA8YQ?q=test
    → API: https://m.avito.ru/api/11/items?key=...&locationId=...&categoryId=...&query=test
    """
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    qs = parse_qs(parsed.query)

    params = {
        "key": "af0deccbgcgidddjgnvljitntccdduijhdinfgjgfjir",
        "page": "1",
        "lastStamp": "",
        "display": "list",
        "limit": "30",
    }

    # Query text
    if "q" in qs:
        params["query"] = qs["q"][0]

    # Price filters
    if "pmin" in qs:
        params["priceMin"] = qs["pmin"][0]
    if "pmax" in qs:
        params["priceMax"] = qs["pmax"][0]

    # Sort
    sort_map = {"104": "date", "101": "priceAsc", "102": "priceDesc"}
    if "s" in qs:
        params["sort"] = sort_map.get(qs["s"][0], "date")
    else:
        params["sort"] = "date"

    # Owner type (private/company)
    if "user" in qs:
        params["owner"] = qs["user"][0]

    # With photo only
    if "bt" in qs:
        params["withImagesOnly"] = "1"

    # The encoded search params from URL (ASgB... part) — pass as searchArea
    # These encode category, location, and other filters
    for part in path_parts:
        if re.match(r'^[A-Z][A-Za-z0-9+/=_-]+$', part) and len(part) > 4:
            params["params"] = part
            break

    # Pass the full original URL path for the API to parse
    # The API accepts the path directly
    params["url"] = parsed.path + ("?" + parsed.query if parsed.query else "")

    return params


async def parse_listings(url: str, max_retries: int = 3) -> list[AvitoItem] | None:
    """Fetch Avito listings using mobile API with fallback to HTML scraping.
    Auto-rotates IP and retries on block."""
    proxy = _get_proxy()
    url = url.replace("m.avito.ru", "www.avito.ru")

    for attempt in range(max_retries):
        delay = random.uniform(config.request_delay_min, config.request_delay_max)
        await asyncio.sleep(delay)

        try:
            loop = asyncio.get_event_loop()

            # Try mobile API first
            result = await loop.run_in_executor(
                None, lambda: _fetch_via_api(url, proxy)
            )
            items, blocked = result

            if blocked:
                logger.warning(
                    "API blocked attempt %d/%d for %s — rotating IP",
                    attempt + 1, max_retries, url[:60],
                )
                await rotate_ip()
                # Longer pause after rotation to let new IP settle
                await asyncio.sleep(random.uniform(3, 7))
                continue

            if items is not None:
                return items

            # Fallback: try HTML scraping
            logger.info("API returned no items, trying HTML for %s", url[:60])
            await asyncio.sleep(random.uniform(2, 5))
            result = await loop.run_in_executor(
                None, lambda: _fetch_html(url, proxy)
            )
            items, blocked = result

            if blocked:
                logger.warning(
                    "HTML also blocked attempt %d/%d — rotating IP",
                    attempt + 1, max_retries,
                )
                await rotate_ip()
                await asyncio.sleep(random.uniform(3, 7))
                continue

            return items

        except Exception as e:
            logger.error("Parse error attempt %d/%d: %s", attempt + 1, max_retries, e)
            if attempt < max_retries - 1:
                await rotate_ip()

    logger.error("All %d attempts failed for %s", max_retries, url[:60])
    return None


def _fetch_via_api(url: str, proxy: str | None) -> tuple[list[AvitoItem] | None, bool]:
    """Fetch listings via Avito mobile API. Returns (items, blocked)."""
    try:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)

        api_url = "https://m.avito.ru/api/11/items"
        params = {
            "key": "af0deccbgcgidddjgnvljitntccdduijhdinfgjgfjir",
            "display": "list",
            "limit": "30",
            "sort": "date",
            "forceLocation": parsed.path,
        }

        if "q" in qs:
            params["query"] = qs["q"][0]
        if "pmin" in qs:
            params["priceMin"] = qs["pmin"][0]
        if "pmax" in qs:
            params["priceMax"] = qs["pmax"][0]

        path_parts = [p for p in path.strip("/").split("/") if p]
        for part in path_parts:
            if re.match(r'^[A-Z][A-Za-z0-9+/=_-]+$', part) and len(part) > 4:
                params["params"] = part
                break

        resp = std_requests.get(
            api_url,
            params=params,
            proxies=_make_proxies(proxy),
            headers={
                "Accept": "application/json",
                "Accept-Language": "ru-RU,ru;q=0.9",
                "User-Agent": "Avito/150.0 (Android 14; Build/AP2A.240805.005)",
                "X-Source": "avito_android",
                "X-Api-Key": "af0deccbgcgidddjgnvljitntccdduijhdinfgjgfjir",
            },
            timeout=30,
        )

        if _is_blocked(resp.text or "", resp.status_code):
            logger.warning("API blocked (HTTP %d) body: %s", resp.status_code, resp.text[:300])
            return (None, True)

        if resp.status_code != 200:
            logger.warning("API HTTP %d for %s, body: %s", resp.status_code, url[:60], resp.text[:300])
            return (None, False)

        data = resp.json()
        items_data = data.get("result", {}).get("items", [])
        if not items_data:
            items_data = data.get("items", [])

        if not items_data:
            logger.info("API returned 0 items for %s, keys=%s", url[:60], list(data.keys())[:5])
            return (None, False)

        items = _parse_api_items(items_data)
        logger.info("API: extracted %d items for %s", len(items), url[:60])
        return (items, False)

    except json.JSONDecodeError:
        logger.warning("API non-JSON for %s, body: %s", url[:60], resp.text[:300] if resp else "?")
        return (None, False)
    except Exception as e:
        logger.error("API fetch failed: %s", e)
        return (None, False)


def _parse_api_items(items_data: list) -> list[AvitoItem]:
    """Parse items from Avito mobile API JSON response."""
    items = []
    for item in items_data:
        if not isinstance(item, dict):
            continue

        # API wraps items in {"type": "item", "value": {...}}
        if "value" in item and isinstance(item["value"], dict):
            item = item["value"]

        avito_id = str(item.get("id", item.get("itemId", "")))
        if not avito_id:
            continue

        title = item.get("title", "Без названия")

        # Price
        price = "Цена не указана"
        price_info = item.get("priceDetailed") or item.get("price")
        if isinstance(price_info, dict):
            price = price_info.get("string") or price_info.get("value", price)
            if isinstance(price, (int, float)):
                price = f"{int(price):,} ₽".replace(",", " ")
        elif isinstance(price_info, str):
            price = price_info
        elif isinstance(price_info, (int, float)):
            price = f"{int(price_info):,} ₽".replace(",", " ")

        # URL
        url_path = item.get("urlPath", item.get("url", ""))
        if url_path and "?" in url_path:
            url_path = url_path.split("?")[0]
        item_url = f"https://www.avito.ru{url_path}" if url_path and not url_path.startswith("http") else url_path

        # Image — API provides multiple sizes
        image_url = None
        images = item.get("images", item.get("photos", []))
        if images:
            if isinstance(images[0], str):
                image_url = images[0]
            elif isinstance(images[0], dict):
                image_url = (
                    images[0].get("636x476")
                    or images[0].get("278x278")
                    or images[0].get("140x140")
                    or images[0].get("url")
                    or images[0].get("src")
                )

        # Location
        location = ""
        loc = item.get("location") or item.get("address") or item.get("geo")
        if isinstance(loc, dict):
            location = loc.get("name", loc.get("formattedAddress", ""))
        elif isinstance(loc, str):
            location = loc

        # Description
        desc = item.get("description", "")
        if isinstance(desc, dict):
            desc = desc.get("text", desc.get("value", ""))
        if desc and len(desc) > 200:
            desc = desc[:200] + "..."

        # Seller info
        seller = item.get("seller", {})
        seller_name = None
        if isinstance(seller, dict):
            seller_name = seller.get("name")

        # Date
        pub_date = item.get("sortTimeStamp") or item.get("time") or item.get("publishDate")
        pub_date_str = None
        if isinstance(pub_date, (int, float)) and pub_date > 1000000000:
            from datetime import datetime, timezone, timedelta
            msk = timezone(timedelta(hours=3))
            pub_date_str = datetime.fromtimestamp(pub_date, msk).strftime("%H:%M %d.%m.%Y")

        items.append(AvitoItem(
            avito_id=avito_id,
            title=title,
            price=str(price),
            url=item_url,
            image_url=image_url,
            location=location or None,
            description=desc or None,
            seller_name=seller_name,
            published_date=pub_date_str,
        ))

    return items


def _fetch_html(url: str, proxy: str | None) -> tuple[list[AvitoItem] | None, bool]:
    """Fallback: fetch HTML page and extract items from embedded JSON.
    Returns (items, blocked)."""
    try:
        session = std_requests.Session()
        session.headers.update(_CHROME_HEADERS)
        resp = session.get(
            url,
            proxies=_make_proxies(proxy),
            timeout=60,
        )

        html = resp.text if resp.text else ""

        if _is_blocked(html, resp.status_code):
            return (None, True)

        if resp.status_code != 200:
            logger.warning("HTML HTTP %d for %s", resp.status_code, url[:60])
            return (None, False)

        # Try embedded JSON extraction
        items = _extract_from_initial_data(html)
        if items is not None:
            logger.info("HTML: extracted %d items from __initialData__", len(items))
            return (items, False)

        items = _extract_from_preloaded_state(html)
        if items is not None:
            logger.info("HTML: extracted %d items from __preloadedState__", len(items))
            return (items, False)

        items = _extract_from_any_json(html)
        if items is not None:
            logger.info("HTML: extracted %d items from embedded JSON", len(items))
            return (items, False)

        json_vars = re.findall(r'window\.(__\w+__)\s*=', html[:50000])
        title_match = re.search(r'<title>([^<]+)</title>', html)
        logger.info("HTML: no items. size=%d, vars=%s, title=%s",
            len(html), json_vars[:5],
            title_match.group(1)[:50] if title_match else "?")

        return ([], False)

    except Exception as e:
        logger.error("HTML fetch failed: %s", e)
        return (None, False)


def _extract_from_initial_data(html: str) -> list[AvitoItem] | None:
    match = re.search(r'window\.__initialData__\s*=\s*"(.+?)"\s*;', html, re.DOTALL)
    if not match:
        return None
    try:
        raw = unquote(match.group(1))
        data = json.loads(raw)
        items_list = _find_items_in_data(data)
        if items_list:
            return _parse_html_items(items_list)
    except Exception as e:
        logger.debug("__initialData__ parse error: %s", e)
    return None


def _extract_from_preloaded_state(html: str) -> list[AvitoItem] | None:
    match = re.search(r'window\.__preloadedState__\s*=\s*({.+?})\s*;', html, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        items_list = _find_items_in_data(data)
        if items_list:
            return _parse_html_items(items_list)
    except Exception as e:
        logger.debug("__preloadedState__ parse error: %s", e)
    return None


def _extract_from_any_json(html: str) -> list[AvitoItem] | None:
    for match in re.finditer(r'"items"\s*:\s*(\[\s*\{.+?\}\s*\])', html[:500000], re.DOTALL):
        try:
            items_data = json.loads(match.group(1))
            if len(items_data) >= 3:
                items = _parse_html_items(items_data)
                if items:
                    return items
        except Exception:
            continue
    return None


def _find_items_in_data(data: dict) -> list | None:
    if not isinstance(data, dict):
        return None
    for key in ["items", "catalog", "results"]:
        val = data.get(key)
        if isinstance(val, list) and len(val) >= 1:
            return val
        if isinstance(val, dict):
            sub = val.get("items") or val.get("list")
            if isinstance(sub, list) and len(sub) >= 1:
                return sub
    for key, val in data.items():
        if isinstance(val, dict):
            for subkey in ["items", "catalog", "results", "list"]:
                sub = val.get(subkey)
                if isinstance(sub, list) and len(sub) >= 1:
                    return sub
    return None


def _parse_html_items(items_data: list) -> list[AvitoItem]:
    """Parse items from HTML-embedded JSON (same as old _parse_items)."""
    items = []
    for item in items_data:
        if not isinstance(item, dict):
            continue
        if "value" in item and isinstance(item["value"], dict):
            item = item["value"]

        avito_id = str(item.get("id", item.get("itemId", "")))
        if not avito_id:
            continue

        title = item.get("title", item.get("name", "Без названия"))

        price_info = item.get("priceDetailed", item.get("price", {}))
        if isinstance(price_info, dict):
            price = price_info.get("string", price_info.get("value", ""))
            if not price:
                val = price_info.get("value", 0)
                price = f"{val} ₽" if val else "Цена не указана"
        elif isinstance(price_info, (int, float)):
            price = f"{int(price_info)} ₽"
        else:
            price = str(price_info) if price_info else "Цена не указана"

        url_path = item.get("urlPath", item.get("url", ""))
        if url_path and "?" in url_path:
            url_path = url_path.split("?")[0]
        item_url = f"https://www.avito.ru{url_path}" if url_path and not url_path.startswith("http") else url_path

        images = item.get("images", item.get("photos", []))
        image_url = None
        if images:
            if isinstance(images[0], str):
                image_url = images[0]
            elif isinstance(images[0], dict):
                image_url = (
                    images[0].get("278x278")
                    or images[0].get("636x476")
                    or images[0].get("140x140")
                    or images[0].get("url")
                    or images[0].get("src")
                )

        loc = item.get("location", item.get("address", item.get("geo", "")))
        if isinstance(loc, dict):
            location = loc.get("name", loc.get("formattedAddress", ""))
        elif isinstance(loc, str):
            location = loc
        else:
            location = ""

        desc = item.get("description", "")
        if isinstance(desc, dict):
            desc = desc.get("text", desc.get("value", ""))
        if desc and len(desc) > 200:
            desc = desc[:200] + "..."

        items.append(AvitoItem(
            avito_id=avito_id,
            title=title,
            price=price,
            url=item_url,
            image_url=image_url,
            location=location or None,
            description=desc or None,
        ))
    return items
