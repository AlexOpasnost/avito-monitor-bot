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


def _extract_category_path(url: str) -> list[str]:
    """Extract category path segments from user's Avito URL.

    /all/odezhda_obuv_aksessuary/muzhskaya_odezhda/verhnyaya_odezhda-ASgB...
    -> ["odezhda_obuv_aksessuary", "muzhskaya_odezhda"]

    These are used to filter items by matching against their urlPath.
    """
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]

    category_parts = []
    for i, part in enumerate(parts):
        if i == 0:
            continue  # skip city (all, moskva, etc)
        # Stop at encoded slugs (contain uppercase)
        if re.search(r'[A-Z]', part):
            # Extract the lowercase prefix before the encoded part
            # e.g. "verhnyaya_odezhda-ASgBAgICA" -> "verhnyaya_odezhda"
            prefix = re.split(r'-[A-Z]', part)[0]
            if prefix and prefix != part:
                category_parts.append(prefix)
            break
        # Skip item URLs
        if re.match(r'^.+_\d{6,}$', part):
            continue
        category_parts.append(part)

    return category_parts


async def parse_listings(url: str) -> list[AvitoItem] | None:
    """Fetch listings from Avito Web API with client-side path-based filtering."""
    proxy = _get_proxy()
    category_path = _extract_category_path(url)

    delay = random.uniform(config.request_delay_min, config.request_delay_max)
    await asyncio.sleep(delay)

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: _fetch_api(url, proxy, category_path)
        )
    except Exception as e:
        logger.error("Parse error for %s: %s", url[:60], e)
        return None


def _fetch_api(url: str, proxy: str | None, category_path: list[str]) -> list[AvitoItem] | None:
    """Fetch from Web API and filter by category path segments."""
    try:
        params = {
            "key": AVITO_API_KEY,
            "sort": "date",
            "display": "list",
            "limit": "50",
            "page": "1",
            "s": "104",
        }

        logger.info("Fetching API, filter_path=%s", category_path)

        resp = curl_requests.get(
            "https://www.avito.ru/web/1/main/items",
            params=params,
            impersonate="chrome",
            proxy=proxy,
            headers={
                "Referer": url,
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

            # URL path
            url_path = item.get("urlPath", "")
            if url_path and "?" in url_path:
                url_path = url_path.split("?")[0]

            # Filter: check that item urlPath contains ALL category segments
            # User URL: /all/odezhda_obuv_aksessuary/muzhskaya_odezhda/...
            # Item urlPath: /moskva/odezhda_obuv_aksessuary/muzhskaya_odezhda/kurtka_12345
            # Match: both contain "odezhda_obuv_aksessuary" AND "muzhskaya_odezhda"
            if category_path:
                item_path_parts = [p for p in url_path.strip("/").split("/") if p]
                match = all(seg in item_path_parts for seg in category_path)
                if not match:
                    continue

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

        logger.info("API returned %d items after filter (raw=%d, path=%s)",
            len(items), len(items_data), category_path)
        return items

    except Exception as e:
        logger.error("API request failed: %s", e)
        return None
