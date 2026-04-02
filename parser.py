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
            prefix = re.split(r'-[A-Z]', part)[0]
            if prefix and prefix != part:
                category_parts.append(prefix)
            break
        # Skip item URLs
        if re.match(r'^.+_\d{6,}$', part):
            continue
        category_parts.append(part)

    # Limit to max 2 segments — deeper subcategories are encoded in slug,
    # not present in item urlPath. E.g. "verhnyaya_odezhda" won't be in urlPath.
    return category_parts[:2]


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
    """Fetch page HTML with session cookies, then try to extract items."""
    try:
        logger.info("Fetching with session, filter_path=%s", category_path)

        # Create session with cookies (like a real browser)
        s = curl_requests.Session(impersonate="chrome")

        # Step 1: Visit main page to get cookies
        try:
            s.get("https://www.avito.ru/", proxy=proxy, timeout=10)
        except Exception:
            pass

        # Step 2: Load the actual user URL (with all filters)
        resp = s.get(
            url,
            proxy=proxy,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Referer": "https://www.avito.ru/",
            },
            timeout=20,
        )

        if resp.status_code == 200 and "captcha" not in resp.text.lower() and "проблема с ip" not in resp.text.lower():
            # Try to parse HTML first (has all filters applied)
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            cards = soup.find_all(attrs={"data-marker": "item"})
            if not cards:
                cards = soup.find_all("div", attrs={"data-item-id": True})

            if cards:
                logger.info("HTML: found %d cards", len(cards))
                items = []
                for card in cards:
                    try:
                        item = _parse_html_card(card)
                        if item:
                            items.append(item)
                    except Exception:
                        continue
                if items:
                    logger.info("Parsed %d items from HTML", len(items))
                    return items

            # If no cards in HTML, try extracting JSON from page
            import re as _re
            json_match = _re.search(r'"items"\s*:\s*(\[.+?\])\s*[,}]', resp.text)
            if json_match:
                try:
                    items_data = json.loads(json_match.group(1))
                    logger.info("Found %d items in embedded JSON", len(items_data))
                    return _parse_json_items(items_data, category_path)
                except Exception:
                    pass

        # Step 3: Fallback to API (no filters but works)
        logger.info("HTML failed (status=%d), falling back to API", resp.status_code)
        resp = s.get(
            "https://www.avito.ru/web/1/main/items",
            params={
                "key": AVITO_API_KEY,
                "sort": "date",
                "display": "list",
                "limit": "50",
                "page": "1",
                "s": "104",
            },
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


def _parse_html_card(card) -> AvitoItem | None:
    """Parse a single item card from HTML."""
    avito_id = card.get("data-item-id", "")
    if not avito_id:
        link = card.find("a", href=re.compile(r"_\d+"))
        if link:
            href = link.get("href", "")
            match = re.search(r"_(\d+)", href.split("?")[0])
            if match:
                avito_id = match.group(1)
    if not avito_id:
        return None

    title_el = card.find(attrs={"data-marker": "item-title"}) or card.find("h3")
    title = title_el.get_text(strip=True) if title_el else "Без названия"

    price_el = card.find(attrs={"data-marker": "item-price"}) or card.find("span", class_=re.compile(r"price", re.I))
    price = price_el.get_text(strip=True) if price_el else "Цена не указана"

    link_el = card.find("a", href=True)
    item_url = ""
    if link_el:
        href = link_el["href"].split("?")[0]
        item_url = href if href.startswith("http") else f"https://www.avito.ru{href}"

    img = card.find("img")
    image_url = None
    if img:
        image_url = img.get("src") or img.get("data-src")
        if image_url and image_url.startswith("//"):
            image_url = "https:" + image_url

    loc_el = card.find(attrs={"data-marker": "item-address"}) or card.find("span", class_=re.compile(r"geo", re.I))
    location = loc_el.get_text(strip=True) if loc_el else None

    return AvitoItem(
        avito_id=str(avito_id), title=title, price=price,
        url=item_url, image_url=image_url, location=location,
    )


def _parse_json_items(items_data: list, category_path: list[str]) -> list[AvitoItem]:
    """Parse items from embedded JSON (no filtering needed — page is already filtered)."""
    items = []
    for item in items_data:
        if not isinstance(item, dict):
            continue
        avito_id = str(item.get("id", ""))
        if not avito_id:
            continue
        title = item.get("title", "Без названия")
        url_path = item.get("urlPath", "").split("?")[0]
        item_url = f"https://www.avito.ru{url_path}" if url_path else ""
        price_info = item.get("priceDetailed", {})
        price = price_info.get("string", "Цена не указана") if isinstance(price_info, dict) else "Цена не указана"
        images = item.get("images", [])
        image_url = None
        if images and isinstance(images[0], dict):
            image_url = images[0].get("278x278") or images[0].get("339x339") or images[0].get("140x140")
        loc = item.get("location", "")
        location = loc.get("name", "") if isinstance(loc, dict) else str(loc) if loc else ""
        if not location and url_path:
            m = re.match(r"/([a-z_-]+)/", url_path)
            if m:
                location = m.group(1).replace("-", " ").replace("_", " ").title()
        items.append(AvitoItem(
            avito_id=avito_id, title=title, price=price,
            url=item_url, image_url=image_url, location=location or None,
        ))
    return items
