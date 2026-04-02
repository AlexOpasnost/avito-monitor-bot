import asyncio
import logging
import random
import re
from dataclasses import dataclass

from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup

from config import config

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; 22081212UG) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
]


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


def _to_mobile_url(url: str) -> str:
    """Convert any avito URL to mobile version."""
    url = url.replace("www.avito.ru", "m.avito.ru")
    url = url.replace("://avito.ru", "://m.avito.ru")
    return url


async def parse_listings(url: str) -> list[AvitoItem] | None:
    """Fetch and parse Avito search page HTML (mobile version).

    Mobile version returns pre-rendered HTML with all filters applied.
    No API needed — just load the page user sees and parse cards.
    """
    proxy = _get_proxy()
    mobile_url = _to_mobile_url(url)

    delay = random.uniform(config.request_delay_min, config.request_delay_max)
    await asyncio.sleep(delay)

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: _fetch_and_parse(mobile_url, proxy)
        )
    except Exception as e:
        logger.error("Parse error for %s: %s", url[:60], e)
        return None


def _fetch_and_parse(url: str, proxy: str | None) -> list[AvitoItem] | None:
    """Fetch mobile Avito page and parse item cards from HTML."""
    try:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

        resp = curl_requests.get(
            url,
            headers=headers,
            proxy=proxy,
            impersonate="chrome",
            timeout=20,
        )

        if resp.status_code == 429:
            logger.warning("HTTP 429 (rate limited) for %s", url[:60])
            return None
        if resp.status_code == 403:
            logger.warning("HTTP 403 (blocked) for %s", url[:60])
            return None
        if resp.status_code != 200:
            logger.warning("HTTP %d for %s", resp.status_code, url[:60])
            return None

        html = resp.text

        # Check for captcha/block
        if "captcha" in html.lower() or "доступ ограничен" in html.lower() or "проблема с ip" in html.lower():
            logger.warning("Captcha/block detected for %s", url[:60])
            return None

        # Parse HTML
        soup = BeautifulSoup(html, "html.parser")
        items = _parse_items_from_html(soup)

        logger.info("Parsed %d items from %s", len(items), url[:60])
        return items

    except Exception as e:
        logger.error("Fetch failed for %s: %s", url[:60], e)
        return None


def _parse_items_from_html(soup: BeautifulSoup) -> list[AvitoItem]:
    """Parse item cards from Avito HTML page."""
    items = []

    # Find item cards — try multiple selectors
    cards = soup.find_all(attrs={"data-marker": "item"})
    if not cards:
        cards = soup.find_all("div", class_=re.compile(r"iva-item"))
    if not cards:
        # Try finding by item links pattern
        cards = soup.find_all("div", attrs={"data-item-id": True})

    for card in cards:
        try:
            item = _parse_single_card(card)
            if item and item.avito_id:
                items.append(item)
        except Exception as e:
            logger.debug("Failed to parse card: %s", e)
            continue

    return items


def _parse_single_card(card) -> AvitoItem | None:
    """Parse a single item card element."""

    # ID
    avito_id = card.get("data-item-id", "")
    if not avito_id:
        # Try from link href
        link = card.find("a", href=re.compile(r"_\d+"))
        if link:
            href = link.get("href", "")
            match = re.search(r"_(\d+)$", href.split("?")[0])
            if match:
                avito_id = match.group(1)
    if not avito_id:
        return None

    # Title
    title_el = (
        card.find(attrs={"data-marker": "item-title"})
        or card.find("h3")
        or card.find("span", class_=re.compile(r"title", re.I))
    )
    title = title_el.get_text(strip=True) if title_el else "Без названия"

    # Price
    price_el = (
        card.find(attrs={"data-marker": "item-price"})
        or card.find("span", class_=re.compile(r"price", re.I))
        or card.find("meta", attrs={"itemprop": "price"})
    )
    if price_el:
        if price_el.get("content"):
            price = f"{price_el['content']} ₽"
        else:
            price = price_el.get_text(strip=True)
    else:
        price = "Цена не указана"

    # URL
    link_el = card.find("a", href=True)
    item_url = ""
    if link_el:
        href = link_el["href"].split("?")[0]  # Remove tracking params
        if href.startswith("http"):
            item_url = href
        else:
            item_url = f"https://www.avito.ru{href}"

    # Image
    image_url = None
    img_el = card.find("img")
    if img_el:
        image_url = img_el.get("src") or img_el.get("data-src")
        if image_url and image_url.startswith("//"):
            image_url = "https:" + image_url

    # Location
    loc_el = (
        card.find(attrs={"data-marker": "item-address"})
        or card.find("span", class_=re.compile(r"geo", re.I))
        or card.find("div", class_=re.compile(r"location", re.I))
    )
    location = loc_el.get_text(strip=True) if loc_el else None

    # Description snippet (if available on list page)
    desc_el = card.find(attrs={"data-marker": "item-description"})
    description = None
    if desc_el:
        description = desc_el.get_text(strip=True)
        if len(description) > 200:
            description = description[:200] + "..."

    return AvitoItem(
        avito_id=str(avito_id),
        title=title,
        price=price,
        url=item_url,
        image_url=image_url,
        location=location,
        description=description,
    )
