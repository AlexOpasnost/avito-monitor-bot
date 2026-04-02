import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

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


def _parse_proxy(proxy_str: str) -> dict:
    """Convert proxy string to Playwright proxy dict (HTTP)."""
    from urllib.parse import urlparse as _urlparse
    p = _urlparse(proxy_str)
    result = {"server": f"http://{p.hostname}:{p.port}"}
    if p.username:
        result["username"] = p.username
    if p.password:
        result["password"] = p.password
    return result


# Playwright singleton
_browser = None
_playwright = None
_lock = asyncio.Lock()


async def _get_browser():
    global _browser, _playwright
    async with _lock:
        if _browser and _browser.is_connected():
            return _browser
        from playwright.async_api import async_playwright
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        logger.info("Playwright browser started")
        return _browser


async def close_playwright():
    global _browser, _playwright
    async with _lock:
        if _browser:
            await _browser.close()
            _browser = None
        if _playwright:
            await _playwright.stop()
            _playwright = None


async def parse_listings(url: str) -> list[AvitoItem] | None:
    """Load Avito search page via Playwright and parse item cards.

    All user filters are preserved because we load the exact same URL.
    """
    # Normalize URL
    url = url.replace("m.avito.ru", "www.avito.ru")

    proxy = _get_proxy()
    proxy_dict = _parse_proxy(proxy) if proxy else None

    try:
        browser = await _get_browser()

        # New context per request (fresh cookies, proxy)
        ctx_kwargs = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
        }
        if proxy_dict:
            ctx_kwargs["proxy"] = proxy_dict

        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()

        try:
            # Load the page
            await page.goto(url, wait_until="networkidle", timeout=30000)

            # Check for captcha
            title = await page.title()
            if "проблема с ip" in title.lower() or "captcha" in title.lower():
                logger.warning("Captcha on %s (title: %s)", url[:60], title[:50])
                return None

            # Wait for items to render
            try:
                await page.wait_for_selector('[data-marker="item"]', timeout=15000)
            except Exception:
                # Maybe different selector or empty results
                logger.info("No [data-marker=item] found, checking page...")
                content = await page.content()
                if "captcha" in content.lower() or "доступ ограничен" in content.lower():
                    logger.warning("Captcha detected in content for %s", url[:60])
                    return None
                # Page loaded but no items — empty search
                logger.info("Empty search results for %s", url[:60])
                return []

            # Parse all item cards
            cards = await page.query_selector_all('[data-marker="item"]')
            logger.info("Found %d cards on %s", len(cards), url[:60])

            items = []
            for card in cards:
                try:
                    item = await _parse_card(card)
                    if item:
                        items.append(item)
                except Exception as e:
                    logger.debug("Card parse error: %s", e)
                    continue

            logger.info("Parsed %d items from %s", len(items), url[:60])
            return items

        finally:
            await page.close()
            await context.close()

    except Exception as e:
        logger.error("Playwright error for %s: %s", url[:60], e)
        return None


async def _parse_card(card) -> AvitoItem | None:
    """Parse a single item card."""
    # ID
    avito_id = await card.get_attribute("data-item-id") or ""
    if not avito_id:
        link = await card.query_selector("a[href]")
        if link:
            href = await link.get_attribute("href") or ""
            m = re.search(r"_(\d+)", href.split("?")[0])
            if m:
                avito_id = m.group(1)
    if not avito_id:
        return None

    # Title
    title_el = await card.query_selector('[data-marker="item-title"]')
    if not title_el:
        title_el = await card.query_selector("h3")
    title = (await title_el.text_content()).strip() if title_el else "Без названия"

    # Price
    price_el = await card.query_selector('[data-marker="item-price"]')
    price = (await price_el.text_content()).strip() if price_el else "Цена не указана"

    # URL
    link = await card.query_selector("a[href]")
    href = (await link.get_attribute("href")) if link else ""
    if href:
        href = href.split("?")[0]
        item_url = href if href.startswith("http") else f"https://www.avito.ru{href}"
    else:
        item_url = ""

    # Image
    img = await card.query_selector("img[src]")
    image_url = (await img.get_attribute("src")) if img else None
    if not image_url:
        img2 = await card.query_selector("img[data-src]")
        image_url = (await img2.get_attribute("data-src")) if img2 else None
    if image_url and image_url.startswith("//"):
        image_url = "https:" + image_url

    # Location
    loc_el = await card.query_selector('[data-marker="item-address"]')
    if not loc_el:
        loc_el = await card.query_selector("span[class*='geo']")
    location = (await loc_el.text_content()).strip() if loc_el else None

    return AvitoItem(
        avito_id=str(avito_id),
        title=title,
        price=price,
        url=item_url,
        image_url=image_url,
        location=location,
    )
