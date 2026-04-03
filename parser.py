import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from urllib.parse import urlparse, unquote

from curl_cffi import requests as curl_requests

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


async def parse_listings(url: str) -> list[AvitoItem] | None:
    """Load Avito search page HTML via curl_cffi + proxy,
    extract items from embedded JSON (window.__initialData__)."""
    proxy = _get_proxy()
    url = url.replace("m.avito.ru", "www.avito.ru")

    delay = random.uniform(config.request_delay_min, config.request_delay_max)
    await asyncio.sleep(delay)

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: _fetch_and_parse(url, proxy))
    except Exception as e:
        logger.error("Parse error: %s", e)
        return None


def _fetch_and_parse(url: str, proxy: str | None) -> list[AvitoItem] | None:
    """Fetch HTML page and extract items from embedded JSON."""
    try:
        # Direct request with impersonate (no warmup — saves time and IP)
        resp = curl_requests.get(
            url,
            proxy=proxy,
            impersonate="chrome",
            headers={
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=60,
        )

        if resp.status_code == 429:
            logger.warning("HTTP 429 for %s", url[:60])
            return None
        if resp.status_code == 403:
            logger.warning("HTTP 403 for %s", url[:60])
            return None
        if resp.status_code != 200:
            logger.warning("HTTP %d for %s", resp.status_code, url[:60])
            return None

        html = resp.text

        if "captcha" in html.lower() or "проблема с ip" in html.lower():
            logger.warning("Captcha for %s", url[:60])
            return None

        # Try to find embedded JSON data
        items = _extract_from_initial_data(html)
        if items is not None:
            logger.info("Extracted %d items from __initialData__", len(items))
            return items

        items = _extract_from_preloaded_state(html)
        if items is not None:
            logger.info("Extracted %d items from __preloadedState__", len(items))
            return items

        items = _extract_from_any_json(html)
        if items is not None:
            logger.info("Extracted %d items from embedded JSON", len(items))
            return items

        # Log what we found for debugging
        json_vars = re.findall(r'window\.(__\w+__)\s*=', html[:50000])
        logger.info("No items found. Page size=%d, JS vars=%s, title=%s",
            len(html), json_vars[:5],
            re.search(r'<title>([^<]+)</title>', html).group(1)[:50] if re.search(r'<title>([^<]+)</title>', html) else "?")

        return []

    except Exception as e:
        logger.error("Fetch failed: %s", e)
        return None


def _extract_from_initial_data(html: str) -> list[AvitoItem] | None:
    """Extract from window.__initialData__ = "URL_ENCODED_JSON";"""
    match = re.search(r'window\.__initialData__\s*=\s*"(.+?)"\s*;', html, re.DOTALL)
    if not match:
        return None

    try:
        raw = unquote(match.group(1))
        data = json.loads(raw)

        # Navigate to items
        items_list = _find_items_in_data(data)
        if items_list:
            return _parse_items(items_list)
    except Exception as e:
        logger.debug("__initialData__ parse error: %s", e)

    return None


def _extract_from_preloaded_state(html: str) -> list[AvitoItem] | None:
    """Extract from window.__preloadedState__ = {...};"""
    match = re.search(r'window\.__preloadedState__\s*=\s*({.+?})\s*;', html, re.DOTALL)
    if not match:
        return None

    try:
        data = json.loads(match.group(1))
        items_list = _find_items_in_data(data)
        if items_list:
            return _parse_items(items_list)
    except Exception as e:
        logger.debug("__preloadedState__ parse error: %s", e)

    return None


def _extract_from_any_json(html: str) -> list[AvitoItem] | None:
    """Try to find items array in any embedded JSON."""
    # Look for "items":[ pattern
    for match in re.finditer(r'"items"\s*:\s*(\[\s*\{.+?\}\s*\])', html[:500000], re.DOTALL):
        try:
            items_data = json.loads(match.group(1))
            if len(items_data) >= 3:
                items = _parse_items(items_data)
                if items:
                    return items
        except Exception:
            continue
    return None


def _find_items_in_data(data: dict) -> list | None:
    """Recursively find items array in nested data structure."""
    if not isinstance(data, dict):
        return None

    # Direct keys
    for key in ["items", "catalog", "results"]:
        val = data.get(key)
        if isinstance(val, list) and len(val) >= 1:
            return val
        if isinstance(val, dict):
            sub = val.get("items") or val.get("list")
            if isinstance(sub, list) and len(sub) >= 1:
                return sub

    # Search one level deep
    for key, val in data.items():
        if isinstance(val, dict):
            for subkey in ["items", "catalog", "results", "list"]:
                sub = val.get(subkey)
                if isinstance(sub, list) and len(sub) >= 1:
                    return sub

    return None


def _parse_items(items_data: list) -> list[AvitoItem]:
    """Parse items from JSON data."""
    items = []
    for item in items_data:
        if not isinstance(item, dict):
            continue

        # Handle "value" wrapper
        if "value" in item and isinstance(item["value"], dict):
            item = item["value"]

        avito_id = str(item.get("id", item.get("itemId", "")))
        if not avito_id:
            continue

        title = item.get("title", item.get("name", "Без названия"))

        # Price
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

        # URL
        url_path = item.get("urlPath", item.get("url", ""))
        if url_path and "?" in url_path:
            url_path = url_path.split("?")[0]
        item_url = f"https://www.avito.ru{url_path}" if url_path and not url_path.startswith("http") else url_path

        # Image
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

        # Location
        loc = item.get("location", item.get("address", item.get("geo", "")))
        if isinstance(loc, dict):
            location = loc.get("name", loc.get("formattedAddress", ""))
        elif isinstance(loc, str):
            location = loc
        else:
            location = ""

        # Description
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
