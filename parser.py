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

# City slug → display name (extracted from URL path)
_CITY_MAP = {
    "moskva": "Москва", "sankt-peterburg": "Санкт-Петербург",
    "novosibirsk": "Новосибирск", "ekaterinburg": "Екатеринбург",
    "kazan": "Казань", "nizhniy_novgorod": "Нижний Новгород",
    "chelyabinsk": "Челябинск", "samara": "Самара", "omsk": "Омск",
    "rostov-na-donu": "Ростов-на-Дону", "ufa": "Уфа",
    "krasnoyarsk": "Красноярск", "voronezh": "Воронеж", "perm": "Пермь",
    "volgograd": "Волгоград", "krasnodar": "Краснодар", "tyumen": "Тюмень",
    "saratov": "Саратов", "tolyatti": "Тольятти", "izhevsk": "Ижевск",
    "barnaul": "Барнаул", "vladivostok": "Владивосток", "irkutsk": "Иркутск",
    "habarovsk": "Хабаровск", "yaroslavl": "Ярославль", "tomsk": "Томск",
    "orenburg": "Оренбург", "novokuznetsk": "Новокузнецк", "ryazan": "Рязань",
    "naberezhnye_chelny": "Набережные Челны", "kirov": "Киров",
    "sevastopol": "Севастополь", "simferopol": "Симферополь",
    "kaliningrad": "Калининград", "bryansk": "Брянск", "tula": "Тула",
    "kursk": "Курск", "stavropol": "Ставрополь", "ulyanovsk": "Ульяновск",
    "tver": "Тверь", "magnitogorsk": "Магнитогорск", "sochi": "Сочи",
    "smolensk": "Смоленск", "murmansk": "Мурманск", "orel": "Орёл",
    "belgorod": "Белгород", "vladimir": "Владимир", "cheboksary": "Чебоксары",
    "kaluga": "Калуга", "surgut": "Сургут", "arhangelsk": "Архангельск",
    "penza": "Пенза", "lipetsk": "Липецк", "tambov": "Тамбов",
    "kemerovo": "Кемерово", "astrahan": "Астрахань", "engels": "Энгельс",
    "kerch": "Керчь", "all": "Вся Россия",
}

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


def _extract_url_filters(url: str) -> dict:
    """Extract filters from Avito search URL (query params + f= decode)."""
    from urllib.parse import urlparse, parse_qs
    import base64
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    filters = {}

    # Price from query params
    if "pmin" in qs:
        try:
            filters["pmin"] = int(qs["pmin"][0])
        except ValueError:
            pass
    if "pmax" in qs:
        try:
            filters["pmax"] = int(qs["pmax"][0])
        except ValueError:
            pass

    # Decode f= parameter to find condition/brand filters
    # Known value IDs from Avito:
    # 5608890 = Новое с биркой, 5608889 = Новое, 5608888 = Б/у
    f_param = qs.get("f", [None])[0]
    if not f_param:
        # Also check in URL path (ASgB... tokens)
        for part in parsed.path.split("/"):
            if part.startswith("ASg") or part.startswith("-ASg"):
                token = part.lstrip("-")
                if "f" not in filters:
                    f_param = token

    if f_param:
        try:
            # URL-safe base64 decode
            padded = f_param + "=" * (4 - len(f_param) % 4)
            raw = base64.urlsafe_b64decode(padded)
            raw_hex = raw.hex()
            # Check for known condition value IDs in binary
            # 5608890 = 0x55962A (varint encoded)
            if b'\xba\xab\xd6\x02' in raw:  # varint for 5608890
                filters["condition"] = "Новое с биркой"
            elif b'\xb9\xab\xd6\x02' in raw:  # 5608889
                filters["condition"] = "Новое"
        except Exception:
            pass

    if filters:
        logger.info("URL filters: %s", filters)

    return filters


def _parse_price_number(price_str: str) -> int | None:
    """Extract numeric price from string like '4 700 ₽'."""
    digits = re.sub(r'[^\d]', '', price_str)
    if digits:
        try:
            return int(digits)
        except ValueError:
            pass
    return None


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

            # Apply price filter from URL params
            if items:
                url_filters = _extract_url_filters(url)
                if url_filters.get("pmin") or url_filters.get("pmax"):
                    before = len(items)
                    filtered = []
                    for item in items:
                        price_num = _parse_price_number(item.price)
                        if price_num is None:
                            filtered.append(item)  # Keep if can't parse price
                            continue
                        if url_filters.get("pmin") and price_num < url_filters["pmin"]:
                            continue
                        if url_filters.get("pmax") and price_num > url_filters["pmax"]:
                            continue
                        filtered.append(item)
                    items = filtered
                    if before != len(items):
                        logger.info("Price filter: %d → %d items (pmin=%s pmax=%s)",
                                    before, len(items),
                                    url_filters.get("pmin"), url_filters.get("pmax"))

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

        # Log the URL being fetched (verify f= param preserved)
        has_f = "?f=" in url or "&f=" in url
        logger.info("Fetching: %s (f= param: %s)", url[:150], "YES" if has_f else "NO")

        resp = scraper.get(
            url,
            proxies=proxies,
            timeout=60,
            allow_redirects=False,  # Don't follow redirects that might strip params
        )

        # If redirect, log where it goes
        if resp.status_code in (301, 302, 303, 307):
            redirect_url = resp.headers.get("Location", "?")
            logger.warning("Redirect %d → %s (f= in redirect: %s)",
                          resp.status_code, redirect_url[:120],
                          "YES" if "f=" in redirect_url else "NO — FILTERS LOST!")
            return (None, True)

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

        if resp.status_code in (301, 302, 303, 307, 308):
            logger.warning("HTTP %d (redirect — likely blocked) for %s", resp.status_code, url[:60])
            return (None, True)  # Treat redirects as blocks — Avito redirects to captcha

        if resp.status_code != 200:
            logger.warning("HTTP %d for %s", resp.status_code, url[:60])
            return (None, True)  # Any non-200 = blocked, trigger IP rotation

        title_match = re.search(r'<title>([^<]+)</title>', html)
        page_title = title_match.group(1)[:80] if title_match else "?"
        logger.info("Page loaded: HTTP %d, size=%d, cookies=%d, title='%s'",
                     resp.status_code, len(html), len(scraper.cookies), page_title)

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


def _truncate_description(text: str, max_len: int = 300) -> str:
    """Truncate description at last sentence boundary within max_len."""
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    # Try to cut at last sentence end
    for sep in ['. ', '! ', '? ', '\n']:
        pos = truncated.rfind(sep)
        if pos > max_len // 3:
            return truncated[:pos + 1].strip()
    # Cut at last space
    pos = truncated.rfind(' ')
    if pos > max_len // 3:
        return truncated[:pos] + "..."
    return truncated + "..."


_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def _convert_relative_date(raw: str, msk) -> str:
    """Convert Avito relative date to absolute datetime string.
    '5 минут назад' → '14:25:00 04.04.2026'
    'Вчера в 10:41' → '10:41:00 03.04.2026'
    '25 марта в 18:17' → '18:17:00 25.03.2026'
    """
    from datetime import datetime as dt, timedelta

    now = dt.now(msk)

    # "X минут назад"
    m = re.match(r'(\d+)\s*минут', raw)
    if m:
        result = now - timedelta(minutes=int(m.group(1)))
        return result.strftime("%H:%M:%S %d.%m.%Y")

    # "X час(а/ов) назад"
    m = re.match(r'(\d+)\s*час', raw)
    if m:
        result = now - timedelta(hours=int(m.group(1)))
        return result.strftime("%H:%M:%S %d.%m.%Y")

    # "X дн(я/ей) назад"
    m = re.match(r'(\d+)\s*(?:день|дня|дней)', raw)
    if m:
        result = now - timedelta(days=int(m.group(1)))
        return result.strftime("%H:%M:%S %d.%m.%Y")

    # "Сегодня в HH:MM"
    m = re.match(r'[Сс]егодня\s*в?\s*(\d{1,2}):(\d{2})', raw)
    if m:
        result = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0)
        return result.strftime("%H:%M:%S %d.%m.%Y")

    # "Вчера в HH:MM"
    m = re.match(r'[Вв]чера\s*в?\s*(\d{1,2}):(\d{2})', raw)
    if m:
        result = (now - timedelta(days=1)).replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0)
        return result.strftime("%H:%M:%S %d.%m.%Y")

    # "25 марта в 18:17"
    m = re.match(r'(\d{1,2})\s+(\w+)\s+в\s+(\d{1,2}):(\d{2})', raw)
    if m:
        day = int(m.group(1))
        month = _MONTHS.get(m.group(2).lower())
        hour, minute = int(m.group(3)), int(m.group(4))
        if month:
            year = now.year
            try:
                result = dt(year, month, day, hour, minute, 0, tzinfo=msk)
                if result > now:  # Must be last year
                    result = dt(year - 1, month, day, hour, minute, 0, tzinfo=msk)
                return result.strftime("%H:%M:%S %d.%m.%Y")
            except ValueError:
                pass

    # "25 марта" (no time)
    m = re.match(r'(\d{1,2})\s+(\w+)$', raw.strip())
    if m:
        day = int(m.group(1))
        month = _MONTHS.get(m.group(2).lower())
        if month:
            year = now.year
            try:
                result = dt(year, month, day, 0, 0, 0, tzinfo=msk)
                if result > now:
                    result = dt(year - 1, month, day, 0, 0, 0, tzinfo=msk)
                return result.strftime("%d.%m.%Y")
            except ValueError:
                pass

    # Already good or unknown format — return as-is if not "назад"
    return raw


async def enrich_item(item: AvitoItem) -> AvitoItem:
    """Fetch individual item page to get full details: exact date, description, views, seller."""
    proxy = _get_proxy()
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: _fetch_item_details(item, proxy))
        return result
    except Exception as e:
        logger.warning("Enrich failed for %s: %s", item.avito_id, e)
        return item


def _fetch_item_details(item: AvitoItem, proxy: str | None) -> AvitoItem:
    """Fetch item detail page and extract rich info."""
    try:
        scraper = _get_session(proxy)
        resp = scraper.get(item.url, proxies=_make_proxies(proxy), timeout=20)

        if resp.status_code != 200:
            return item

        html = resp.text
        if len(html) < 10000:
            return item

        # Exact publication date — ALWAYS overwrite listing date with detail page date
        from datetime import datetime as dt, timezone as tz, timedelta
        msk = tz(timedelta(hours=3))
        detail_date = None  # Track separately, overwrite at end

        # 1. Unix timestamp in JSON: "time":1712345678 or "createTime":...
        ts_match = re.search(r'"(?:time|createTime|sortTimeStamp|publishDate)"\s*:\s*(\d{10,13})', html)
        if ts_match:
            try:
                ts = int(ts_match.group(1))
                if ts > 1_000_000_000_000:  # milliseconds
                    ts = ts // 1000
                parsed = dt.fromtimestamp(ts, msk)
                detail_date = parsed.strftime("%H:%M:%S %d.%m.%Y")
            except Exception:
                pass

        # 2. ISO date in JSON: "datePublished":"2026-..."
        if not detail_date:
            iso_match = re.search(r'"(?:datePublished|date|createdAt|created)"\s*:\s*"(20\d{2}-\d{2}-\d{2}[T ]\d{2}:\d{2}[^"]*)"', html)
            if iso_match:
                try:
                    raw = iso_match.group(1).strip()
                    parsed = dt.fromisoformat(raw.replace("Z", "+00:00"))
                    detail_date = parsed.astimezone(msk).strftime("%H:%M:%S %d.%m.%Y")
                except Exception:
                    pass

        # 3. <time datetime="...">
        if not detail_date:
            time_match = re.search(r'<time[^>]*datetime="([^"]+)"', html)
            if time_match:
                try:
                    parsed = dt.fromisoformat(time_match.group(1).replace("Z", "+00:00"))
                    detail_date = parsed.astimezone(msk).strftime("%H:%M:%S %d.%m.%Y")
                except Exception:
                    pass

        # 4. Text near item ID: "№ 8109418734 · 26 марта в 18:17"
        if not detail_date:
            date_text_match = re.search(
                rf'№\s*{re.escape(item.avito_id)}[^<]*?·\s*([^·<]+?)(?:\s*·|\s*<)',
                html
            )
            if date_text_match:
                raw_date = date_text_match.group(1).strip()
                detail_date = _convert_relative_date(raw_date, msk)

        # 5. Broader search: any "DD month в HH:MM" or "Сегодня/Вчера в HH:MM" on page
        if not detail_date:
            for pattern in [
                r'(\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+в\s+\d{1,2}:\d{2})',
                r'([Сс]егодня\s+в\s+\d{1,2}:\d{2})',
                r'([Вв]чера\s+в\s+\d{1,2}:\d{2})',
            ]:
                m = re.search(pattern, html)
                if m:
                    detail_date = _convert_relative_date(m.group(1).strip(), msk)
                    break

        # Overwrite with detail page date (more accurate than listing)
        if detail_date:
            item.published_date = detail_date

        # Description
        desc_match = re.search(r'data-marker="item-view/item-description"[^>]*>(.*?)</div>', html, re.DOTALL)
        if not desc_match:
            desc_match = re.search(r'itemprop="description"[^>]*>(.*?)</div>', html, re.DOTALL)
        if desc_match:
            # Strip HTML tags
            desc_text = re.sub(r'<[^>]+>', ' ', desc_match.group(1))
            desc_text = re.sub(r'\s+', ' ', desc_text).strip()
            if desc_text and len(desc_text) > 5:
                item.description = _truncate_description(desc_text)

        # Views
        views_match = re.search(r'data-marker="item-view/total-views"[^>]*>([^<]+)<', html)
        if not views_match:
            views_match = re.search(r'(\d+)\s*просмотр', html)
        if views_match:
            item.views = views_match.group(1).strip()

        # Seller name
        seller_match = re.search(r'data-marker="seller-info/name"[^>]*>.*?<a[^>]*>([^<]+)<', html, re.DOTALL)
        if not seller_match:
            seller_match = re.search(r'data-marker="seller-info/name"[^>]*>([^<]+)<', html)
        if seller_match:
            item.seller_name = seller_match.group(1).strip()

        # Seller rating
        rating_match = re.search(r'data-marker="seller-info/score"[^>]*>([^<]+)<', html)
        if not rating_match:
            rating_match = re.search(r'(\d[.,]\d)\s*<.*?(\d+)\s*отзыв', html, re.DOTALL)
        if rating_match:
            item.seller_rating = rating_match.group(1).strip()

        # Location (more precise from detail page)
        loc_match = re.search(r'data-marker="delivery/location"[^>]*>([^<]+)<', html)
        if not loc_match:
            loc_match = re.search(r'data-marker="item-view/item-address"[^>]*>([^<]+)<', html)
        if not loc_match:
            loc_match = re.search(r'class="style-item-address[^"]*"[^>]*>.*?>([^<]{3,60})<', html, re.DOTALL)
        if loc_match:
            item.location = loc_match.group(1).strip()

        # Extract item attributes from JSON on detail page
        # Format: "Состояние"...{"attributeId":115539,...,"description":"Новое"}
        attrs = {}
        for attr_label in ["Состояние", "Бренд", "Размер", "Цвет", "Тип"]:
            # Find label, then grab the next "description":"value" after it
            m = re.search(
                rf'{attr_label}.*?"description"\s*:\s*"([^"]+)"',
                html[:500000], re.DOTALL
            )
            if m:
                val = m.group(1).strip()
                # Skip if value is too long (grabbed wrong field)
                if len(val) < 50:
                    attrs[attr_label] = val

        if attrs:
            logger.info("Item %s attrs: %s", item.avito_id, attrs)

        # Store attrs for filtering (used by scheduler)
        item._attrs = attrs

        logger.info("Enriched %s: date=%s views=%s seller=%s desc=%d chars",
                     item.avito_id,
                     item.published_date or "-",
                     item.views or "-",
                     item.seller_name or "-",
                     len(item.description) if item.description else 0)
        return item

    except Exception as e:
        logger.warning("Detail fetch error for %s: %s", item.avito_id, e)
        return item


def _extract_from_html_items(html: str) -> list[AvitoItem] | None:
    """Extract items from HTML by splitting on data-item-id blocks.
    Only parses items from the search results container (catalog-serp),
    ignoring 'recommended' and 'similar' sections below."""

    # Limit to search results container — ignore recommendations
    serp_match = re.search(r'data-marker="catalog-serp"', html)
    if serp_match:
        # Find the end of the serp container (next major section)
        serp_start = serp_match.start()
        # Look for recommendation/similar sections that follow
        serp_end = len(html)
        for end_marker in [
            'data-marker="recommended',
            'data-marker="similar',
            'data-marker="catalog-rubricator"',
            'Похожие объявления',
            'Рекомендуем посмотреть',
            'Вы недавно смотрели',
        ]:
            pos = html.find(end_marker, serp_start + 100)
            if pos != -1 and pos < serp_end:
                serp_end = pos
        search_html = html[serp_start:serp_end]
        logger.info("SERP container: %d chars (full page: %d)", len(search_html), len(html))

        # Find active filters on the search page
        # Look for filter-related markers and selected values
        filter_markers = re.findall(r'data-marker="([^"]*(?:filter|param|chip)[^"]*)"', html, re.IGNORECASE)
        if filter_markers:
            logger.info("Filter markers found: %s", filter_markers[:15])

        # Look for "applied filters" / chips / selected values
        # Avito shows active filters as "chips" (tags) at the top
        chips = re.findall(r'data-marker="applied-filters/item[^"]*"[^>]*>.*?>([^<]{2,40})<', html, re.DOTALL)
        if chips:
            logger.info("Applied filter chips: %s", chips[:10])

        # Extract checked checkbox labels from filter sidebar
        checked_filters = {}
        # Find checkbox markers and capture 300 chars after for label
        for cb_match in re.finditer(r'data-marker="params\[(\d+)\]/checkbox/(\d+)"', html):
            param_id = cb_match.group(1)
            value_id = cb_match.group(2)
            # Get 300 chars around the marker to find checked state + label
            start = max(0, cb_match.start() - 100)
            end = min(len(html), cb_match.end() + 300)
            block = html[start:end]

            is_checked = ('checked' in block.lower() or
                         'aria-checked="true"' in block or
                         '"isChecked":true' in block or
                         'iva-checkbox-checked' in block)
            if not is_checked:
                continue

            # Find label: text in spans/divs after the checkbox
            # Skip very short text (1 char) and HTML entities
            label = None
            after_marker = html[cb_match.end():cb_match.end() + 300]
            for text_match in re.finditer(r'>([^<]{2,50})<', after_marker):
                txt = text_match.group(1).strip()
                if txt and not txt.startswith('id:') and len(txt) > 1:
                    label = txt
                    break
            if not label:
                label = f"val:{value_id}"

            if param_id not in checked_filters:
                checked_filters[param_id] = []
            if label not in checked_filters[param_id]:
                checked_filters[param_id].append(label)

        if checked_filters:
            logger.info("CHECKED filters: %s", checked_filters)

        # Find applied filter chips/tags at the top of results
        # These show text like "Nike", "Новое с биркой", "от 1000 ₽"
        applied_chips = []
        # Method 1: applied-filters markers
        for chip_match in re.finditer(r'data-marker="applied-filters[^"]*"[^>]*>(.*?)</(?:span|div|button)>', html, re.DOTALL):
            texts = re.findall(r'>([^<]{2,40})<', chip_match.group(1))
            applied_chips.extend([t.strip() for t in texts if t.strip() and t.strip() != '×'])

        # Method 2: filter chips near "Выбранные фильтры" or reset button
        if not applied_chips:
            chips_section = re.search(r'(?:params\[\d+\]-reset|Сбросить|Выбранные)(.*?)(?:data-marker="search-filters"|catalog-serp)', html, re.DOTALL)
            if chips_section:
                chip_texts = re.findall(r'>([А-Яа-яёA-Za-z][^<]{2,40})<', chips_section.group(1))
                applied_chips = [t.strip() for t in chip_texts if t.strip() not in ('Сбросить', 'Показать', 'Ещё')]

        # Method 3: look for text inside toggle buttons that are "on"
        if not applied_chips:
            for toggle in re.finditer(r'data-marker="params\[\d+\]/checkbox/toggle"[^>]*value="(\d+)"(.*?)</div>\s*</div>', html, re.DOTALL):
                # Get text from surrounding divs
                block = toggle.group(2)
                texts = re.findall(r'>([А-Яа-яёA-Za-z][^<]{2,40})<', block)
                if texts:
                    applied_chips.extend([t.strip() for t in texts])

        if applied_chips:
            logger.info("Applied filter values: %s", applied_chips[:20])

        # Dump HTML around first checkbox for structure analysis
        first_cb = re.search(r'(data-marker="params\[\d+\]/checkbox/toggle"[^>]*value="\d+".{0,500})', html, re.DOTALL)
        if first_cb:
            sample = first_cb.group(1)[:400].replace('\n', ' ').replace('\r', '')
            logger.info("Toggle checkbox+context: %s", sample)
    else:
        # Fallback: use first 60% of page (results are at top, recommendations at bottom)
        search_html = html[:int(len(html) * 0.6)]
        logger.info("No catalog-serp marker, using first 60%% of page")

    # Find all item IDs within search results only
    id_matches = list(re.finditer(r'data-item-id="(\d+)"', search_html))
    if len(id_matches) < 3:
        return None

    logger.info("Found %d data-item-id in HTML", len(id_matches))

    items = []
    for i, match in enumerate(id_matches):
        avito_id = match.group(1)

        # Extract block: from this data-item-id to the next one (or +5000 chars)
        start = match.start()
        end = id_matches[i + 1].start() if i + 1 < len(id_matches) else start + 5000
        block = search_html[start:end]

        # Title + URL — from link with title attribute containing the item name
        title = "Объявление"
        url_path = None

        # Best: link inside item-title marker with title attr
        title_match = re.search(r'data-marker="item-title"[^>]*>.*?href="([^"]*)"[^>]*?title="([^"]*)"', block, re.DOTALL)
        if title_match:
            url_path = title_match.group(1)
            title = title_match.group(2).strip()

        # Alt: link with title attr that contains the avito_id in href
        if not title_match:
            title_match = re.search(rf'href="(/[^"]*{avito_id}[^"]*)"[^>]*?title="([^"]*)"', block)
            if title_match:
                url_path = title_match.group(1)
                title = title_match.group(2).strip()

        # Alt: any link with a meaningful title attr (not "Добавить")
        if title == "Объявление":
            for t in re.finditer(r'title="([^"]{5,120})"', block):
                t_text = t.group(1).strip()
                if "избранное" not in t_text.lower() and "сравнение" not in t_text.lower() and "добавить" not in t_text.lower():
                    title = t_text
                    break

        # URL fallback: any link with the avito_id
        if not url_path:
            url_match = re.search(rf'href="(/[^"]*?{avito_id}[^"]*?)"', block)
            if url_match:
                url_path = url_match.group(1)

        # Clean URL — remove tracking query params
        if url_path and "?" in url_path:
            url_path = url_path.split("?")[0]
        item_url = f"https://www.avito.ru{url_path}" if url_path else f"https://www.avito.ru/{avito_id}"

        # Price — data-marker="item-price-value"
        price = "Цена не указана"
        price_match = re.search(r'data-marker="item-price-value"[^>]*>([^<]+)<', block)
        if not price_match:
            price_match = re.search(r'data-marker="item-price"[^>]*>.*?(\d[\d\s]*\d)\s*₽', block, re.DOTALL)
        if not price_match:
            price_match = re.search(r'(\d[\d\s]*\d)\s*₽', block)
        if price_match:
            price = price_match.group(1).strip()
            if '₽' not in price:
                price += ' ₽'

        # Location — extract from URL path (most reliable)
        location = None
        if url_path:
            city_slug = url_path.strip("/").split("/")[0] if url_path else None
            if city_slug:
                location = _CITY_MAP.get(city_slug)
        # Also try data-marker
        if not location:
            loc_match = re.search(r'data-marker="item-location"[^>]*>([^<]+)<', block)
            if not loc_match:
                loc_container = re.search(r'data-marker="item-location"(.*?)</div>', block, re.DOTALL)
                if loc_container:
                    loc_texts = re.findall(r'>([^<]{2,})<', loc_container.group(1))
                    if loc_texts:
                        location = ", ".join(t.strip() for t in loc_texts if t.strip() and t.strip() != title)
            else:
                location = loc_match.group(1).strip()

        # Image — from img.avito.st CDN or any image source
        image_url = None
        img_match = re.search(r'(?:src|data-src)="(https://\d+\.img\.avito\.st/image/[^"]+)"', block)
        if not img_match:
            img_match = re.search(r'(?:src|data-src)="(https://[^"]*avito\.st/[^"]+)"', block)
        if not img_match:
            # slider-image marker contains URL in its value
            img_match = re.search(r'data-marker="slider-image/image-(https://[^"]+)"', block)
        if not img_match:
            img_match = re.search(r'(?:src|data-src)="(https://[^"]*\.(?:jpg|jpeg|webp|png)(?:\?[^"]*)?)"', block)
        if img_match:
            image_url = img_match.group(1)

        # Description — look for text content that's not title/price/location
        description = None
        # Try data-marker="item-description" first (may not exist on list view)
        desc_match = re.search(r'data-marker="item-description"[^>]*>([^<]{5,})<', block)
        if desc_match:
            description = _truncate_description(desc_match.group(1).strip())

        # Date from listing + age filter
        listing_date = None
        date_container = re.search(r'data-marker="item-date[^"]*"(.*?)</div>', block, re.DOTALL)
        if date_container:
            date_texts = re.findall(r'>([^<]+)<', date_container.group(1))
            listing_date = " ".join(t.strip() for t in date_texts if t.strip())

        # Skip items 2+ days old
        if listing_date:
            days_match = re.search(r'(\d+)\s*(?:день|дня|дней)', listing_date)
            if days_match and int(days_match.group(1)) >= 2:
                continue
            if re.search(r'(?:недел|месяц|год)', listing_date):
                continue

        # Convert listing date to absolute
        from datetime import timezone as tz, timedelta
        msk = tz(timedelta(hours=3))
        converted_date = _convert_relative_date(listing_date, msk) if listing_date else None

        items.append(AvitoItem(
            avito_id=avito_id,
            title=title,
            price=price,
            url=item_url,
            image_url=image_url,
            location=location,
            description=description,
            published_date=converted_date,
        ))

    # Log first item for debugging
    if items:
        first = items[0]
        logger.info("HTML item[0]: id=%s title='%s' price='%s' loc='%s' img=%s url=%s",
                     first.avito_id, first.title[:50], first.price[:20],
                     first.location or "-", "yes" if first.image_url else "no",
                     first.url[:70])

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
