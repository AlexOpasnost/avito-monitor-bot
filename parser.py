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


def _extract_search_params(url: str) -> dict:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    params = {
        "key": AVITO_API_KEY,
        "sort": "date",
        "page": "1",
        "display": "list",
        "limit": "30",
    }
    for key in ["s", "pmin", "pmax", "q", "cd", "context", "f"]:
        if key in qs:
            params[key] = qs[key][0]
    if "s" not in params:
        params["s"] = "104"
    return params


async def parse_listings(url: str) -> list[AvitoItem] | None:
    """Fetch listings from Avito internal API."""
    proxy = _get_proxy()
    params = _extract_search_params(url)

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
        return await loop.run_in_executor(None, lambda: _fetch_api(referer, params, proxy))
    except Exception as e:
        logger.error("Parse error for %s: %s", url, e)
        return None


def _fetch_api(referer: str, params: dict, proxy: str | None) -> list[AvitoItem] | None:
    try:
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

            # Location
            loc = item.get("location", "")
            if isinstance(loc, dict):
                location = loc.get("name", "") or loc.get("formattedAddress", "")
            elif isinstance(loc, str):
                location = loc
            else:
                location = ""

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

        logger.info("API returned %d items", len(items))
        return items

    except Exception as e:
        logger.error("API request failed: %s", e)
        return None


def enrich_item(item: AvitoItem, proxy: str | None) -> AvitoItem:
    """Enrich item with description, seller, date from mobile API v19."""
    try:
        s = curl_requests.Session(impersonate="chrome")
        s.headers.update({
            "user-agent": "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 Chrome/124.0.0.0 Mobile Safari/537.36",
            "accept": "application/json",
            "accept-language": "ru-RU,ru;q=0.9",
        })
        # Get cookies
        s.get("https://m.avito.ru/", proxy=proxy, timeout=8)

        resp = s.get(
            f"https://m.avito.ru/api/19/items/{item.avito_id}",
            params={"key": AVITO_MOBILE_KEY},
            proxy=proxy,
            timeout=10,
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
