import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs, unquote

import cloudscraper
import requests as std_requests

from config import config

logger = logging.getLogger(__name__)

# Persistent session — reuse cookies across requests
_session: cloudscraper.CloudScraper | None = None
_session_created_at: float = 0
_SESSION_MAX_AGE = 300  # recreate session every 5 min


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


def _make_proxies(proxy: str | None) -> dict | None:
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


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
                    # Invalidate session after IP rotation — need fresh cookies
                    _invalidate_session()
                    await asyncio.sleep(3)
                    return True
                else:
                    body = await resp.text()
                    logger.warning("IP rotation HTTP %d: %s", resp.status, body[:200])
                    return False
    except Exception as e:
        logger.warning("IP rotation failed: %s", e)
        return False


async def check_proxy_ip() -> str | None:
    """Check what IP the proxy is actually using."""
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


def _invalidate_session():
    """Force session recreation on next request."""
    global _session, _session_created_at
    _session = None
    _session_created_at = 0


def _get_session(proxy: str | None) -> cloudscraper.CloudScraper:
    """Get or create a cloudscraper session with cookies."""
    global _session, _session_created_at

    now = time.time()
    if _session and (now - _session_created_at) < _SESSION_MAX_AGE:
        return _session

    logger.info("Creating new cloudscraper session...")
    scraper = cloudscraper.create_scraper(
        browser={
            "browser": "chrome",
            "platform": "windows",
            "desktop": True,
        },
    )

    proxies = _make_proxies(proxy)

    # Warm up: visit Avito homepage to get session cookies
    # If blocked, the caller will rotate IP and invalidate session
    try:
        warmup_resp = scraper.get(
            "https://www.avito.ru/",
            proxies=proxies,
            timeout=30,
        )
        cookies_count = len(scraper.cookies)
        logger.info(
            "Session warmup: HTTP %d, %d cookies, size=%d",
            warmup_resp.status_code, cookies_count, len(warmup_resp.text),
        )

        # If warmup itself is blocked, don't cache this session
        if warmup_resp.status_code in (403, 429) or len(warmup_resp.text) < 50000:
            blocked_check, reason = _is_blocked(warmup_resp.text, warmup_resp.status_code)
            if blocked_check:
                logger.warning("Warmup blocked (%s) — session not cached", reason)
                return scraper  # Return but don't cache
    except Exception as e:
        logger.warning("Session warmup failed: %s", e)
        return scraper  # Return uncached

    _session = scraper
    _session_created_at = now
    return scraper


def _is_blocked(html: str, status_code: int) -> tuple[bool, str]:
    """Check if Avito blocked the request. Returns (blocked, reason).

    Important: normal Avito pages (~1MB) contain 'captcha' in JS scripts.
    Real block pages are small (<50KB) with specific titles.
    """
    if status_code in (403, 429):
        return (True, f"HTTP {status_code}")

    if not html:
        return (False, "")

    # Extract title for precise check
    title_match = re.search(r'<title>([^<]*)</title>', html[:5000], re.IGNORECASE)
    title = title_match.group(1).lower() if title_match else ""

    # Block page has specific title
    if "доступ ограничен" in title or "проблема с ip" in title:
        return (True, f"title '{title[:50]}'")

    # Only check keywords on small pages (real block pages are <50KB)
    # Normal Avito search pages are 500KB-2MB
    if len(html) < 50000:
        html_lower = html.lower()
        for keyword in ["geetest", "проблема с ip", "доступ ограничен"]:
            if keyword in html_lower:
                return (True, f"small page + keyword '{keyword}'")
        # Check captcha only in title or very small pages
        if len(html) < 10000 and "captcha" in html_lower:
            return (True, "captcha on tiny page")

    return (False, "")


async def parse_listings(url: str, max_retries: int = 5) -> list[AvitoItem] | None:
    """Fetch Avito listings. Strategy:
    1. Use cloudscraper session with cookies (warm up on avito.ru first)
    2. Parse HTML for embedded JSON data
    3. On block: rotate IP, invalidate session, retry
    """
    proxy = _get_proxy()
    url = url.replace("m.avito.ru", "www.avito.ru")

    for attempt in range(max_retries):
        delay = random.uniform(config.request_delay_min, config.request_delay_max)
        await asyncio.sleep(delay)

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: _fetch_with_session(url, proxy)
            )
            items, blocked = result

            if blocked:
                logger.warning(
                    "Blocked attempt %d/%d for %s — rotating IP",
                    attempt + 1, max_retries, url[:60],
                )
                _invalidate_session()
                await rotate_ip()
                # Progressive backoff: wait longer on each retry
                wait = 5 + attempt * 5  # 5s, 10s, 15s
                await asyncio.sleep(random.uniform(wait, wait + 5))
                continue

            return items

        except Exception as e:
            logger.error("Parse error attempt %d/%d: %s", attempt + 1, max_retries, e)
            if attempt < max_retries - 1:
                _invalidate_session()
                await rotate_ip()
                await asyncio.sleep(5)

    logger.error("All %d attempts failed for %s", max_retries, url[:60])
    return None


def _fetch_with_session(url: str, proxy: str | None) -> tuple[list[AvitoItem] | None, bool]:
    """Fetch Avito page using cloudscraper session with cookies."""
    try:
        scraper = _get_session(proxy)
        proxies = _make_proxies(proxy)

        resp = scraper.get(
            url,
            proxies=proxies,
            timeout=60,
        )

        html = resp.text or ""

        blocked, reason = _is_blocked(html, resp.status_code)
        if blocked:
            title_match = re.search(r'<title>([^<]+)</title>', html)
            title = title_match.group(1) if title_match else "?"
            # Log first 500 chars to understand what Avito returns
            preview = html[:500].replace('\n', ' ').replace('\r', '')
            logger.warning("Blocked (HTTP %d) reason=%s title='%s' cookies=%d preview='%s'",
                           resp.status_code, reason, title[:50], len(scraper.cookies), preview)
            return (None, True)

        if resp.status_code != 200:
            logger.warning("HTTP %d for %s", resp.status_code, url[:60])
            return (None, False)

        logger.info("Page loaded: HTTP %d, size=%d, cookies=%d",
                     resp.status_code, len(html), len(scraper.cookies))

        # Try to extract items from embedded JSON
        items = _extract_from_initial_data(html)
        if items is not None:
            logger.info("Extracted %d items from __initialData__", len(items))
            return (items, False)

        items = _extract_from_preloaded_state(html)
        if items is not None:
            logger.info("Extracted %d items from __preloadedState__", len(items))
            return (items, False)

        items = _extract_from_mfe(html)
        if items is not None:
            logger.info("Extracted %d items from __mfe__", len(items))
            return (items, False)

        items = _extract_from_any_json(html)
        if items is not None:
            logger.info("Extracted %d items from embedded JSON", len(items))
            return (items, False)

        # Try HTML data attributes (iva-item-root)
        items = _extract_from_html_items(html)
        if items is not None:
            logger.info("Extracted %d items from HTML data-attrs", len(items))
            return (items, False)

        # Debug: log what's on the page
        json_vars = re.findall(r'window\.(__\w+__)\s*=', html[:50000])
        title_match = re.search(r'<title>([^<]+)</title>', html)
        # Sample some data keys for debugging
        sample = ""
        for var_name in json_vars[:3]:
            m = re.search(rf'window\.{var_name}\s*=\s*', html)
            if m:
                snippet = html[m.end():m.end()+200]
                sample += f" {var_name}={snippet[:100]}..."
        logger.info("No items found. size=%d, vars=%s, title=%s, sample=%s",
                     len(html), json_vars[:5],
                     title_match.group(1)[:60] if title_match else "?",
                     sample[:300])

        return ([], False)

    except Exception as e:
        logger.error("Fetch failed: %s", e)
        return (None, False)


# --- JSON extraction from HTML ---

def _extract_from_initial_data(html: str) -> list[AvitoItem] | None:
    match = re.search(r'window\.__initialData__\s*=\s*', html)
    if not match:
        return None
    try:
        data = _parse_js_value(html, match.end())
        if not data:
            return None
        logger.info("__initialData__ keys: %s", list(data.keys())[:10])
        items_list = _find_items_in_data(data)
        if items_list:
            return _parse_items(items_list)
        items_list = _deep_find_items(data)
        if items_list:
            return _parse_items(items_list)
    except Exception as e:
        logger.debug("__initialData__ parse error: %s", e)
    return None


def _extract_from_preloaded_state(html: str) -> list[AvitoItem] | None:
    # Note: Avito uses __preloadedState_ (single underscore!) not __preloadedState__
    # Value can be raw JSON {..} OR url-encoded string "..."
    for pattern in [r'window\.__preloadedState__\s*=\s*', r'window\.__preloadedState_\s*=\s*']:
        match = re.search(pattern, html)
        if match:
            break
    else:
        return None
    try:
        data = _parse_js_value(html, match.end())
        if not data:
            return None
        top_keys = list(data.keys())
        logger.info("__preloadedState keys: %s", top_keys[:15])

        # Log structure of promising keys to find where listings hide
        for key in top_keys:
            val = data[key]
            if isinstance(val, dict):
                sub_keys = list(val.keys())[:10]
                # Check sizes to find the big data blob
                size = len(json.dumps(val, ensure_ascii=False)) if len(sub_keys) > 0 else 0
                if size > 10000:  # Only log big objects (likely contain data)
                    logger.info("  preloadedState.%s (%d bytes) keys: %s", key, size, sub_keys)
            elif isinstance(val, list):
                logger.info("  preloadedState.%s is list[%d]", key, len(val))

        items_list = _find_items_in_data(data)
        if items_list:
            return _parse_items(items_list)
        items_list = _deep_find_items(data)
        if items_list:
            return _parse_items(items_list)
    except Exception as e:
        logger.warning("__preloadedState parse error: %s", e)
    return None


def _extract_from_mfe(html: str) -> list[AvitoItem] | None:
    """Extract items from window.__mfe__ (modern Avito micro-frontend data)."""
    match = re.search(r'window\.__mfe__\s*=\s*', html)
    if not match:
        return None
    try:
        data = _parse_js_value(html, match.end())
        if not data:
            return None
        logger.info("__mfe__ keys: %s", list(data.keys())[:10])

        # Log deeper structure of __mfe__
        for key in list(data.keys())[:5]:
            val = data[key]
            if isinstance(val, dict):
                for k2 in list(val.keys())[:10]:
                    v2 = val[k2]
                    if isinstance(v2, dict):
                        size = len(json.dumps(v2, ensure_ascii=False))
                        if size > 5000:
                            logger.info("  mfe.%s.%s (%d bytes) keys: %s",
                                        key, k2, size, list(v2.keys())[:10])

        items_list = _deep_find_items(data)
        if items_list:
            return _parse_items(items_list)
    except Exception as e:
        logger.warning("__mfe__ parse error: %s", e)
    return None


def _parse_js_value(html: str, pos: int) -> dict | None:
    """Parse a JS value that can be either raw JSON {..} or URL-encoded string "..."."""
    if pos >= len(html):
        return None

    ch = html[pos]

    if ch == '{':
        # Raw JSON object
        json_str = _extract_json_object(html, pos)
        if json_str:
            return json.loads(json_str)

    elif ch == '"':
        # URL-encoded string: "...encoded..."
        end = html.find('";', pos + 1)
        if end == -1:
            end = html.find('"', pos + 1)
        if end == -1:
            return None
        encoded = html[pos + 1:end]
        decoded = unquote(encoded)
        return json.loads(decoded)

    return None


def _extract_json_object(html: str, start: int) -> str | None:
    """Extract a complete JSON object starting at position `start` in html.
    Uses bracket counting instead of regex to handle nested objects correctly."""
    if start >= len(html) or html[start] != '{':
        return None

    depth = 0
    in_string = False
    escape = False
    i = start

    # Limit scan to 10MB to avoid infinite loop
    end_limit = min(len(html), start + 10_000_000)

    while i < end_limit:
        ch = html[i]
        if escape:
            escape = False
            i += 1
            continue
        if ch == '\\' and in_string:
            escape = True
            i += 1
            continue
        if ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return html[start:i + 1]
        i += 1
    return None


def _extract_from_html_items(html: str) -> list[AvitoItem] | None:
    """Extract items from HTML by splitting on data-item-id blocks."""
    # Find all item IDs
    id_matches = list(re.finditer(r'data-item-id="(\d+)"', html))
    if len(id_matches) < 3:
        return None

    logger.info("Found %d data-item-id in HTML", len(id_matches))

    items = []
    for i, match in enumerate(id_matches):
        avito_id = match.group(1)

        # Extract block: from this data-item-id to the next one (or +5000 chars)
        start = match.start()
        end = id_matches[i + 1].start() if i + 1 < len(id_matches) else start + 5000
        block = html[start:end]

        # Title: first <a> with href containing the item ID or /item path
        title = "Объявление"
        title_match = re.search(r'href="(/[^"]*?)"[^>]*?title="([^"]*)"', block)
        if not title_match:
            # Try: text inside link that has href with path
            title_match = re.search(r'href="(/[^"]*?)"[^>]*>([^<]{3,80})<', block)
        if title_match:
            url_path = title_match.group(1)
            title = title_match.group(2).strip()
        else:
            url_path = None

        # Try to get URL from any link containing the avito_id
        if not url_path:
            url_match = re.search(rf'href="(/[^"]*?{avito_id}[^"]*?)"', block)
            if url_match:
                url_path = url_match.group(1)

        item_url = f"https://www.avito.ru{url_path}" if url_path else f"https://www.avito.ru/{avito_id}"

        # Price: look for price patterns (digits + ₽ or "руб")
        price = "Цена не указана"
        price_match = re.search(r'(\d[\d\s]*\d)\s*₽', block)
        if not price_match:
            price_match = re.search(r'data-marker="item-price"[^>]*>([^<]+)<', block)
        if not price_match:
            price_match = re.search(r'price[^>]*>([^<]*\d[^<]*)<', block, re.IGNORECASE)
        if price_match:
            price = price_match.group(1).strip()
            if not price.endswith('₽'):
                price += ' ₽'

        # Location — look for specific Avito geo markers
        location = None
        loc_match = re.search(r'data-marker="item-address"[^>]*>([^<]{3,60})<', block)
        if not loc_match:
            loc_match = re.search(r'class="[^"]*geo-address[^"]*"[^>]*>([^<]{3,60})<', block)
        if not loc_match:
            loc_match = re.search(r'class="[^"]*item-address[^"]*"[^>]*>([^<]{3,60})<', block)
        if not loc_match:
            # Look for city patterns like "Москва, район" but NOT the title
            loc_match = re.search(r'>([А-Я][а-яё]+(?:,\s*[А-Яа-яё\s]+(?:район|р-н|обл|край))[^<]{0,30})<', block)
        if loc_match:
            loc_text = loc_match.group(1).strip()
            # Verify it's not the title repeated
            if loc_text != title and len(loc_text) < 60:
                location = loc_text

        # Image — try multiple sources for lazy-loaded images
        image_url = None
        # 1. data-src (lazy loaded)
        img_match = re.search(r'data-src="(https://[^"]*\.(?:jpg|jpeg|png|webp)[^"]*)"', block, re.IGNORECASE)
        # 2. Regular src with avito CDN
        if not img_match:
            img_match = re.search(r'src="(https://(?:\d+\.)?avito\.st/[^"]+)"', block, re.IGNORECASE)
        # 3. Any image src with common image CDN patterns
        if not img_match:
            img_match = re.search(r'(?:src|data-src)="(https://[^"]*(?:/items|/images|/thumbs)[^"]*)"', block, re.IGNORECASE)
        if img_match:
            image_url = img_match.group(1)

        items.append(AvitoItem(
            avito_id=avito_id,
            title=title,
            price=price,
            url=item_url,
            image_url=image_url,
            location=location,
        ))

    # Log first item for debugging
    if items:
        first = items[0]
        logger.info("HTML first item: id=%s title='%s' price='%s' url=%s img=%s",
                     first.avito_id, first.title[:40], first.price[:20],
                     first.url[:60], "yes" if first.image_url else "no")

    return items if items else None


def _extract_from_any_json(html: str) -> list[AvitoItem] | None:
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


def _is_real_listing(item: dict) -> bool:
    """Check if a dict looks like a real Avito listing (not a category/nav item).
    Real listings have: id + (urlPath or price or images)."""
    if "value" in item and isinstance(item["value"], dict):
        item = item["value"]
    has_id = "id" in item or "itemId" in item
    has_url = bool(item.get("urlPath"))
    has_price = "price" in item or "priceDetailed" in item
    has_images = bool(item.get("images") or item.get("photos"))
    # Must have ID and at least one listing-specific field
    return has_id and (has_url or has_price or has_images)


def _deep_find_items(data, depth: int = 0, max_depth: int = 8) -> list | None:
    """Recursively search for items array in nested data (up to max_depth)."""
    if depth > max_depth:
        return None

    if isinstance(data, dict):
        if "id" in data and ("title" in data or "name" in data):
            return None  # Single item, not a list

        for key in ["items", "catalog", "results", "list", "mainItems", "snippets"]:
            val = data.get(key)
            if isinstance(val, list) and len(val) >= 3:
                # Check that first items look like real listings
                sample = val[:3]
                real_count = sum(1 for v in sample if isinstance(v, dict) and _is_real_listing(v))
                if real_count >= 2:
                    logger.info("Found listings at depth=%d key='%s' count=%d", depth, key, len(val))
                    return val

        # Recurse into dict values
        for key, val in data.items():
            result = _deep_find_items(val, depth + 1, max_depth)
            if result:
                return result

    elif isinstance(data, list) and len(data) >= 3:
        sample = data[:3]
        real_count = sum(1 for v in sample if isinstance(v, dict) and _is_real_listing(v))
        if real_count >= 2:
            return data

    return None


def _parse_items(items_data: list) -> list[AvitoItem]:
    """Parse items from JSON data."""
    # Log first item structure for debugging
    if items_data:
        first = items_data[0]
        if isinstance(first, dict) and "value" in first:
            first = first["value"]
        if isinstance(first, dict):
            logger.info("First item keys: %s", list(first.keys())[:15])
            logger.info("First item sample: id=%s title=%s urlPath=%s price=%s",
                        first.get("id"), str(first.get("title", ""))[:30],
                        str(first.get("urlPath", ""))[:50], first.get("priceDetailed") or first.get("price"))

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

        # Price
        price_info = item.get("priceDetailed", item.get("price", {}))
        if isinstance(price_info, dict):
            price = price_info.get("string", price_info.get("value", ""))
            if not price:
                val = price_info.get("value", 0)
                price = f"{val} ₽" if val else "Цена не указана"
        elif isinstance(price_info, (int, float)):
            price = f"{int(price_info):,} ₽".replace(",", " ")
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
                    images[0].get("636x476")
                    or images[0].get("278x278")
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

        # Seller
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
