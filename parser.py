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


async def parse_listings(url: str) -> list[AvitoItem] | None:
    """Fetch listings from Avito Web API using key= param with full path."""
    proxy = _get_proxy()

    delay = random.uniform(config.request_delay_min, config.request_delay_max)
    await asyncio.sleep(delay)

    # Normalize to www.avito.ru
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host == "m.avito.ru":
        url = url.replace("m.avito.ru", "www.avito.ru")
        parsed = urlparse(url)

    # key= is the full path from URL (with encoded slug — contains category info)
    path = parsed.path  # e.g. /all/odezhda.../verhnyaya_odezhda-ASgBAgICAkTeAtgL4ALeCw
    qs = parse_qs(parsed.query)

    referer = url

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: _fetch_api(path, qs, referer, proxy)
        )
    except Exception as e:
        logger.error("Parse error for %s: %s", url, e)
        return None


def _fetch_api(path: str, qs: dict, referer: str, proxy: str | None) -> list[AvitoItem] | None:
    try:
        params = {
            "key": path,  # Full path with encoded slug = exact category filter
            "sort": "date",
            "display": "list",
            "limit": "50",
            "page": "1",
        }

        # Pass f, s, cd and other params from original URL
        for k, v in qs.items():
            if k not in params:
                params[k] = v[0]

        if "s" not in params:
            params["s"] = "104"

        logger.info("API request: key=%s", path[:80])

        resp = curl_requests.get(
            "https://www.avito.ru/web/1/main/items",
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
        if not items_data:
            return []

        items = []
        for item in items_data:
            if not isinstance(item, dict):
                continue

            avito_id = str(item.get("id", ""))
            if not avito_id:
                continue

            title = item.get("title", "Без названия")

            # URL
            url_path = item.get("urlPath", "")
            if url_path and "?" in url_path:
                url_path = url_path.split("?")[0]
            item_url = f"https://www.avito.ru{url_path}" if url_path else ""

            # Price
            price_info = item.get("priceDetailed", {})
            if isinstance(price_info, dict):
                price = price_info.get("string", "") or "Цена не указана"
            else:
                price = "Цена не указана"

            # Image
            images = item.get("images", [])
            image_url = None
            if images and isinstance(images[0], dict):
                image_url = (
                    images[0].get("278x278")
                    or images[0].get("339x339")
                    or images[0].get("140x140")
                )

            # Location
            loc = item.get("location", "")
            if isinstance(loc, dict):
                location = loc.get("name", "")
            elif isinstance(loc, str):
                location = loc
            else:
                location = ""

            # Fallback location from URL
            if not location and url_path:
                city_match = re.match(r"/([a-z_-]+)/", url_path)
                if city_match:
                    location = city_match.group(1).replace("-", " ").replace("_", " ").title()

            items.append(AvitoItem(
                avito_id=avito_id,
                title=title,
                price=price,
                url=item_url,
                image_url=image_url,
                location=location or None,
            ))

        logger.info("API returned %d items", len(items))
        return items

    except Exception as e:
        logger.error("API request failed: %s", e)
        return None
