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


def _extract_category_slug(url: str) -> str | None:
    """Extract category slug from Avito URL path for client-side filtering."""
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]

    # Skip city (first part), return category (second part)
    # e.g. /all/telefony/mobile-ASgB... -> "telefony"
    # e.g. /moskva/kvartiry -> "kvartiry"
    for i, part in enumerate(path_parts):
        if i == 0:
            continue  # skip city
        # Skip encoded slugs
        if re.search(r'[A-Z]', part):
            continue
        if re.match(r'^.+_\d{6,}$', part):
            continue
        return part
    return None


async def parse_listings(url: str) -> list[AvitoItem] | None:
    """Fetch listings from Avito Web API with client-side category filtering."""
    proxy = _get_proxy()
    category_slug = _extract_category_slug(url)

    delay = random.uniform(config.request_delay_min, config.request_delay_max)
    await asyncio.sleep(delay)

    # Normalize referer
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host == "m.avito.ru":
        referer = url.replace("m.avito.ru", "www.avito.ru")
    else:
        referer = url

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: _fetch_api(referer, proxy, category_slug)
        )
    except Exception as e:
        logger.error("Parse error for %s: %s", url, e)
        return None


def _fetch_api(referer: str, proxy: str | None, category_slug: str | None) -> list[AvitoItem] | None:
    try:
        params = {
            "key": AVITO_API_KEY,
            "sort": "date",
            "display": "list",
            "limit": "50",
            "page": "1",
            "s": "104",
        }

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

            # URL path — used for filtering
            url_path = item.get("urlPath", "")
            if url_path and "?" in url_path:
                url_path = url_path.split("?")[0]

            # Filter by category slug in urlPath
            if category_slug and url_path:
                path_parts = [p for p in url_path.strip("/").split("/") if p]
                # Category is usually the second part: /city/category/item_name
                item_category = path_parts[1] if len(path_parts) >= 2 else ""
                if item_category != category_slug:
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

        logger.info("API returned %d items (filter=%s, total_raw=%d)",
            len(items), category_slug, len(items_data))
        return items

    except Exception as e:
        logger.error("API request failed: %s", e)
        return None
