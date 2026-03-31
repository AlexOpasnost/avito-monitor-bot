import asyncio
import logging
import random
import re
from dataclasses import dataclass

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
    return random.choice(config.proxy_list)


# ---------------------------------------------------------------------------
# Playwright browser singleton
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


# ---------------------------------------------------------------------------
# Parsing a single card
# ---------------------------------------------------------------------------

async def _parse_card(card) -> AvitoItem | None:
    """Parse a single listing card element into AvitoItem."""
    try:
        # ID
        item_id = await card.get_attribute("data-item-id") or ""
        if not item_id:
            try:
                link = await card.query_selector("a[href]")
                if link:
                    href = await link.get_attribute("href") or ""
                    match = re.search(r"_(\d+)$", href)
                    if match:
                        item_id = match.group(1)
            except Exception:
                pass

        if not item_id:
            return None

        # Title
        title = "Без названия"
        try:
            title_el = await card.query_selector('[data-marker="item-title"]')
            if not title_el:
                title_el = await card.query_selector("h3")
            if title_el:
                title = (await title_el.text_content() or "").strip() or "Без названия"
        except Exception:
            pass

        # Price
        price = "Цена не указана"
        try:
            price_el = await card.query_selector('[data-marker="item-price"]')
            if price_el:
                price = (await price_el.text_content() or "").strip() or "Цена не указана"
        except Exception:
            pass

        # Image
        image_url = None
        try:
            img = await card.query_selector("img[src]")
            if img:
                image_url = await img.get_attribute("src")
            if not image_url:
                img2 = await card.query_selector("img[data-src]")
                if img2:
                    image_url = await img2.get_attribute("data-src")
        except Exception:
            pass

        # URL
        item_url = ""
        try:
            link = await card.query_selector('a[href*="/"]')
            if link:
                url_path = await link.get_attribute("href") or ""
                if url_path and not url_path.startswith("http"):
                    item_url = f"https://www.avito.ru{url_path}"
                else:
                    item_url = url_path
        except Exception:
            pass

        # Location
        location = None
        try:
            loc_el = await card.query_selector('[data-marker="item-address"]')
            if not loc_el:
                loc_el = await card.query_selector('[class*="geo"]')
            if loc_el:
                location = (await loc_el.text_content() or "").strip() or None
        except Exception:
            pass

        return AvitoItem(
            avito_id=item_id,
            title=title,
            price=price,
            url=item_url,
            image_url=image_url,
            location=location,
        )

    except Exception as e:
        logger.debug("Failed to parse card: %s", e)
        return None


# ---------------------------------------------------------------------------
# Main listing parser
# ---------------------------------------------------------------------------

async def parse_listings(url: str) -> list[AvitoItem] | None:
    """Fetch listings from Avito via Playwright (real browser rendering)."""
    try:
        browser = await _get_browser()

        # Create context with proxy
        context_kwargs = {
            "user_agent": (
                "Mozilla/5.0 (Linux; Android 13; SM-S908B) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Mobile Safari/537.36"
            ),
            "locale": "ru-RU",
        }
        if config.proxy_list:
            context_kwargs["proxy"] = _parse_proxy_for_playwright(config.proxy_list[0])

        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        try:
            # Random delay before request
            delay = random.uniform(config.request_delay_min, config.request_delay_max)
            await asyncio.sleep(delay)

            await page.goto(url, wait_until="domcontentloaded", timeout=20000)

            # Check for captcha / block page
            content = await page.content()
            content_lower = content.lower()
            if "captcha" in content_lower or "доступ ограничен" in content_lower or "проблема с ip" in content_lower:
                logger.warning("Playwright: captcha/block on %s", url[:60])
                return None

            # Wait for listing cards
            try:
                await page.wait_for_selector('[data-marker="item"]', timeout=15000)
            except Exception:
                logger.info("Playwright: no [data-marker=item] found on %s", url[:60])
                return []

            cards = await page.query_selector_all('[data-marker="item"]')

            items: list[AvitoItem] = []
            for card in cards:
                item = await _parse_card(card)
                if item:
                    items.append(item)

            logger.info("Playwright: found %d items on %s", len(items), url[:60])
            return items

        finally:
            await page.close()
            await context.close()

    except Exception as e:
        logger.error("Playwright parse error for %s: %s", url[:60], e)
        return None
