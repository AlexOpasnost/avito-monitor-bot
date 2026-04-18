"""Avito parser — primary: mobile API, fallback: HTML hydration JSON."""
import html as html_lib
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs

import httpx
import orjson

from config import config

logger = logging.getLogger(__name__)


@dataclass
class AvitoItem:
    avito_id: str
    title: str
    price: str
    price_value: int | None
    url: str
    image_url: str | None
    location: str | None
    description: str | None
    seller_name: str | None
    published_timestamp: int | None  # unix seconds


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def fetch_search_items(url: str, proxy: str | None, max_retries: int = 3) -> list[AvitoItem] | None:
    """Try mobile API first, fall back to HTML hydration JSON.
    Rotates IP and retries on blocks (429/403)."""
    import asyncio

    for attempt in range(max_retries):
        # Try mobile API first (fast, clean JSON)
        items, api_blocked = await _fetch_mobile_api(url, proxy)
        if items is not None:
            logger.info("[api] fetched %d items for %s", len(items), url[:80])
            return items

        # API blocked — rotate IP FIRST, then try HTML with fresh IP + fresh session
        if api_blocked:
            logger.info("[api] blocked, rotating IP before HTML fallback...")
            _invalidate_session()
            await rotate_ip()
            await asyncio.sleep(8)

        # Try HTML fallback with (possibly fresh) IP
        items, html_blocked = await _fetch_hydration_json(url, proxy)
        if items is not None:
            logger.info("[html] fetched %d items for %s", len(items), url[:80])
            return items

        if not api_blocked and not html_blocked:
            return None

        if html_blocked:
            logger.warning("HTML also blocked (attempt %d/%d), rotating IP again", attempt + 1, max_retries)
            _invalidate_session()
            await rotate_ip()
            await asyncio.sleep(8)

    logger.error("All %d attempts blocked for %s", max_retries, url[:80])
    return None


async def rotate_ip() -> bool:
    """Call proxy rotation URL (if configured). Logs body so we can
    verify the API actually rotated and isn't silently returning 200
    with 'already rotating' or a rate-limit response."""
    if not config.proxy_rotate_url:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(config.proxy_rotate_url)
            body = (resp.text or "").strip()[:300]
            logger.info(
                "[proxy] changeip HTTP %d, body=%r",
                resp.status_code, body,
            )
            return resp.status_code == 200
    except Exception as e:
        logger.warning("rotate_ip failed: %s", e)
        return False


async def check_proxy_ip() -> str | None:
    """Return external IP via the configured proxy, or None."""
    if not config.proxy_list:
        return None
    try:
        async with httpx.AsyncClient(proxy=config.proxy_list[0], timeout=15) as client:
            resp = await client.get("https://api.ipify.org?format=json")
            if resp.status_code == 200:
                return resp.json().get("ip")
    except Exception as e:
        logger.warning("check_proxy_ip failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Mobile API
# ---------------------------------------------------------------------------

async def _fetch_mobile_api(url: str, proxy: str | None) -> tuple[list[AvitoItem] | None, bool]:
    """Fetch via the Avito mobile API using curl_cffi (Chrome TLS fingerprint).
    Returns (items, blocked). blocked=True means IP is rate-limited/banned."""
    import asyncio
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    api_url = "https://m.avito.ru/api/11/items"
    params = {
        "key": config.avito_api_key,
        "locationId": "621540",
        "limit": "50",
        "display": "list",
        "sort": "date",
    }
    if "f" in qs:
        params["f"] = qs["f"][0]
    if "q" in qs:
        params["query"] = qs["q"][0]
    if "pmin" in qs:
        params["priceMin"] = qs["pmin"][0]
    if "pmax" in qs:
        params["priceMax"] = qs["pmax"][0]

    def _do_request():
        from urllib.parse import urlencode
        full_url = api_url + "?" + urlencode(params)
        try:
            scraper = _get_cloudscraper(proxy)
            proxies = {"http": proxy, "https": proxy} if proxy else None
            req_ua = scraper.headers.get("User-Agent", "?")
            logger.info("[api] request UA=%s", req_ua)
            resp = scraper.get(
                full_url,
                proxies=proxies,
                headers={
                    "Accept": "application/json",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                    "Referer": "https://m.avito.ru/",
                },
                timeout=30,
            )
            return resp.status_code, resp.text, dict(resp.headers)
        except Exception as e:
            logger.debug("[api] cloudscraper error: %s", e)
            return 0, "", {}

    try:
        loop = asyncio.get_running_loop()
        status, text, resp_headers = await loop.run_in_executor(None, _do_request)

        if status in (429, 403):
            logger.warning(
                "[api] BLOCKED %d for %s — response headers: %s",
                status, url[:80], resp_headers,
            )
            return None, True
        if status != 200:
            logger.debug("[api] HTTP %d for %s", status, url[:80])
            return None, False

        data = orjson.loads(text)
        items_raw = (data.get("result") or {}).get("items") or []
        items: list[AvitoItem] = []
        for raw in items_raw:
            if raw.get("type") != "item":
                continue
            val = raw.get("value") or {}
            try:
                items.append(_parse_api_item(val))
            except Exception as e:
                logger.debug("parse item err: %s", e)
        if items:
            logger.info("[api] SUCCESS: %d items from mobile API", len(items))
        return (items if items else None), False
    except Exception as e:
        logger.debug("[api] exception: %s", e)
        return None, False


# ---------------------------------------------------------------------------
# HTML fallback (hydration JSON)
# ---------------------------------------------------------------------------

_cs_session = None
_cs_created = 0.0

# Modern UAs — cloudscraper's built-in pool has Chrome 54/62/66 (2017-18)
# which Avito easily fingerprints as outdated. Override with current ones.
_MODERN_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def _get_cloudscraper(proxy: str | None):
    """Get or create a cloudscraper session (reuse for cookies)."""
    import random
    import time
    global _cs_session, _cs_created
    now = time.time()
    if _cs_session and (now - _cs_created) < 300:
        return _cs_session
    import cloudscraper
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True}
    )
    # Override cloudscraper's outdated UA pool (Chrome 54/62/66) with a
    # modern one so Avito doesn't flag the request on fingerprint alone.
    modern_ua = random.choice(_MODERN_USER_AGENTS)
    s.headers["User-Agent"] = modern_ua
    proxies = {"http": proxy, "https": proxy} if proxy else None

    # Log the UA override — should stay the same for warmup AND main requests
    session_ua = s.headers.get("User-Agent", "?")
    logger.info("[html] session id=%s, User-Agent=%s", id(s), session_ua)

    # Verify proxy is actually routing traffic — same session, same proxy
    try:
        ip_r = s.get("https://api.ipify.org?format=json", proxies=proxies, timeout=15)
        if ip_r.status_code == 200:
            logger.info("[html] pre-warmup visible IP via proxy: %s", (ip_r.json() or {}).get("ip", "?"))
        else:
            logger.warning("[html] ipify returned HTTP %d", ip_r.status_code)
    except Exception as e:
        logger.warning("[html] ipify probe failed: %s", str(e)[:120])

    # Warmup: visit BOTH domains for cookies
    try:
        r = s.get("https://www.avito.ru/", proxies=proxies, timeout=20)
        logger.info(
            "[html] warmup www: HTTP %d, %d cookies, UA=%s",
            r.status_code, len(s.cookies), r.request.headers.get("User-Agent", "?"),
        )
    except Exception as e:
        logger.warning("[html] warmup www failed: %s", e)
    try:
        r2 = s.get("https://m.avito.ru/", proxies=proxies, timeout=20)
        logger.info(
            "[html] warmup m: HTTP %d, %d cookies, UA=%s",
            r2.status_code, len(s.cookies), r2.request.headers.get("User-Agent", "?"),
        )
    except Exception as e:
        logger.warning("[html] warmup m failed: %s", e)

    # Diagnostic — which cookies did Avito give us?
    try:
        cookie_names = sorted({c.name for c in s.cookies})
        cookie_domains = sorted({c.domain for c in s.cookies})
        logger.info(
            "[html] session cookies after warmup: %d (names=%s, domains=%s)",
            len(s.cookies), cookie_names[:12], cookie_domains,
        )
    except Exception as e:
        logger.debug("[html] cookie dump err: %s", e)

    # Cooldown — bumped to 15-25s. 3-7s wasn't enough; Avito's rate-limit
    # sliding window seems to be ~10-15s for a mobile-proxy IP that just
    # did 3 consecutive connects (ipify + www + m). Mimic a human reading
    # the main page before clicking through to search.
    cooldown = random.uniform(15, 25)
    logger.info("[html] warmup cooldown %.1fs", cooldown)
    time.sleep(cooldown)
    _cs_session = s
    _cs_created = now
    return s


def _fetch_html_sync(url: str, proxy: str | None):
    """Fetch Avito HTML with cloudscraper (sync, runs in thread).
    Returns (status, html, response_headers) or None on error."""
    try:
        s = _get_cloudscraper(proxy)
        proxies = {"http": proxy, "https": proxy} if proxy else None
        req_ua = s.headers.get("User-Agent", "?")
        logger.info("[html] request UA=%s, session id=%s", req_ua, id(s))
        resp = s.get(url, proxies=proxies, timeout=60, allow_redirects=False)
        return resp.status_code, resp.text, dict(resp.headers)
    except Exception as e:
        logger.debug("[html] sync fetch error: %s", e)
        return None


def _invalidate_session():
    """Reset cloudscraper session (after IP rotation)."""
    global _cs_session, _cs_created
    _cs_session = None
    _cs_created = 0.0


_HYDRATION_RE = re.compile(
    r'<script[^>]*data-mfe-state="true"[^>]*>([^<]+)</script>',
    re.IGNORECASE,
)


async def _fetch_hydration_json(url: str, proxy: str | None) -> tuple[list[AvitoItem] | None, bool]:
    """Fetch HTML with cloudscraper (bypasses WAF) + extract hydration JSON."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        resp_data = await loop.run_in_executor(None, lambda: _fetch_html_sync(url, proxy))
        if resp_data is None:
            return None, False
        status, html, resp_headers = resp_data
        if status in (429, 403):
            logger.warning(
                "[html] BLOCKED %d for %s — response headers: %s",
                status, url[:80], resp_headers,
            )
            return None, True
        if status in (301, 302, 303, 307, 308):
            logger.warning("[html] REDIRECT %d (block) for %s", status, url[:80])
            return None, True
        if status != 200:
            logger.debug("[html] HTTP %d for %s", status, url[:80])
            return None, False
        if "проблема с ip" in html.lower() or "доступ ограничен" in html.lower():
            logger.warning("[html] BLOCKED (IP problem) for %s", url[:80])
            return None, True
        logger.info("[html] Page loaded: %d, size=%d", status, len(html))
    except Exception as e:
        logger.debug("[html] exception: %s", e)
        return None, False

    # Method 1: data-mfe-state hydration JSON (Duff89 method — BeautifulSoup)
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.select("script"):
            if (script.get("type") == "mime/invalid"
                and script.get("data-mfe-state") == "true"
                and "sandbox" not in (script.text or "")):
                data = orjson.loads(html_lib.unescape(script.text))
                if data.get("i18n", {}).get("hasMessages", {}):
                    catalog = data.get("state", {}).get("data", {}).get("catalog", {})
                    items_raw = catalog.get("items") or []
                    items: list[AvitoItem] = []
                    for raw in items_raw:
                        if isinstance(raw, dict) and raw.get("type") and raw.get("type") != "item":
                            continue
                        val = raw.get("value") if isinstance(raw, dict) and "value" in raw else raw
                        if not isinstance(val, dict):
                            continue
                        try:
                            items.append(_parse_api_item(val))
                        except Exception:
                            pass
                    if items:
                        logger.info("[html] parsed %d items from mfe-state (Duff89 method)", len(items))
                        return items, False
                    else:
                        logger.info("[html] mfe-state found (i18n OK) but catalog empty, keys: %s",
                                   list(data.get("state", {}).get("data", {}).keys())[:8])
    except ImportError:
        logger.debug("[html] BeautifulSoup not installed, skipping mfe-state method")
    except Exception as e:
        logger.debug("[html] mfe-state parse err: %s", e)

    # Method 2: __initialData__ (URL-encoded JSON in older Avito pages)
    from urllib.parse import unquote
    init_match = re.search(r'window\.__initialData__\s*=\s*"(.+?)"\s*;', html, re.DOTALL)
    if init_match:
        try:
            raw_json = unquote(init_match.group(1))
            data = orjson.loads(raw_json)
            items = _extract_items_from_json(data)
            if items:
                logger.info("[html] parsed %d items from __initialData__", len(items))
                return items, False
        except Exception as e:
            logger.debug("[html] __initialData__ parse err: %s", e)

    # Method 3: __preloadedState__ or __mfe__ (URL-encoded JSON in quotes)
    for var_name in ['__preloadedState__', '__preloadedState_', '__mfe__']:
        marker = f'window.{var_name}'
        idx = html.find(marker)
        if idx < 0:
            continue
        # Find the opening quote after =
        eq_idx = html.find('=', idx)
        if eq_idx < 0:
            continue
        # Skip whitespace after =
        start = eq_idx + 1
        while start < len(html) and html[start] in ' \t\n\r':
            start += 1
        if start >= len(html):
            continue

        try:
            if html[start] == '"':
                # URL-encoded string: "...encoded..."
                end = html.find('";', start + 1)
                if end < 0:
                    end = html.find('"', start + 1)
                if end > start:
                    encoded = html[start + 1:end]
                    decoded = unquote(encoded)
                    data = orjson.loads(decoded)
            elif html[start] == '{':
                # Raw JSON object — find matching }
                depth = 0
                i = start
                while i < min(len(html), start + 5_000_000):
                    if html[i] == '{':
                        depth += 1
                    elif html[i] == '}':
                        depth -= 1
                        if depth == 0:
                            data = orjson.loads(html[start:i + 1])
                            break
                    i += 1
                else:
                    continue
            else:
                continue

            items = _extract_items_from_json(data)
            if items:
                logger.info("[html] parsed %d items from %s", len(items), var_name)
                return items, False
            else:
                # Log deeper structure to find where items hide
                keys_info = list(data.keys())[:8] if isinstance(data, dict) else "?"
                logger.info("[html] %s found but no items (keys: %s)", var_name, keys_info)
                # Dig into first-level values
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, dict):
                            sub_keys = list(v.keys())[:8]
                            size = len(str(v))
                            if size > 5000:
                                logger.info("[html]   %s.%s (%d chars): %s", var_name, k, size, sub_keys)
                                # Try second level
                                for k2, v2 in v.items():
                                    if isinstance(v2, dict) and len(str(v2)) > 5000:
                                        logger.info("[html]     %s.%s.%s (%d chars): %s",
                                                   var_name, k, k2, len(str(v2)), list(v2.keys())[:8])
        except Exception as e:
            logger.debug("[html] %s parse error: %s", var_name, e)

    # Debug: what JS vars and scripts are on the page?
    js_vars = re.findall(r'window\.(__\w+__)\s*=', html[:50000])
    mfe_scripts = re.findall(r'data-mfe-state', html[:50000])
    logger.info("[html] JS vars: %s, mfe-state tags: %d, page size: %d",
                js_vars[:5], len(mfe_scripts), len(html))

    # Method 4: data-item-id from HTML (last resort)
    item_ids = re.findall(r'data-item-id="(\d+)"', html)
    if len(item_ids) >= 3:
        items = []
        for item_id in item_ids:
            # Extract minimal info: title from nearby link
            idx = html.find(f'data-item-id="{item_id}"')
            block = html[idx:idx + 3000] if idx >= 0 else ""
            title_m = re.search(r'title="([^"]{5,80})"', block)
            title = title_m.group(1) if title_m else f"Объявление {item_id}"
            # Skip junk titles
            if "избранное" in title.lower() or "сравнение" in title.lower():
                title_m2 = re.search(r'href="[^"]*"[^>]*>([^<]{5,80})<', block)
                title = title_m2.group(1).strip() if title_m2 else f"Объявление {item_id}"
            url_m = re.search(rf'href="(/[^"]*?{item_id}[^"]*?)"', block)
            url_path = url_m.group(1).split("?")[0] if url_m else f"/{item_id}"
            price_m = re.search(r'(\d[\d\s]*\d)\s*₽', block)
            price = (price_m.group(1).strip() + " ₽") if price_m else ""
            items.append(AvitoItem(
                avito_id=item_id, title=title, price=price, price_value=None,
                url=f"https://www.avito.ru{url_path}",
                image_url=None, location=None, description=None,
                seller_name=None, published_timestamp=None,
            ))
        logger.info("[html] parsed %d items from data-item-id", len(items))
        return items, False

    logger.warning("[html] no items found in HTML (size=%d)", len(html))
    return None, False


# ---------------------------------------------------------------------------
# Item parsing
# ---------------------------------------------------------------------------

def _extract_items_from_json(data: dict) -> list[AvitoItem]:
    """Recursively find items array in a nested JSON structure."""
    # Try common paths
    for path in [
        lambda d: d.get("items", []),
        lambda d: d.get("catalog", {}).get("items", []),
        lambda d: d.get("results", []),
    ]:
        items_raw = path(data)
        if isinstance(items_raw, list) and len(items_raw) >= 3:
            items = []
            for raw in items_raw:
                if not isinstance(raw, dict):
                    continue
                val = raw.get("value", raw) if "value" in raw else raw
                if not isinstance(val, dict):
                    continue
                # Must have id + urlPath or price (real listing, not category)
                if not (val.get("id") or val.get("itemId")):
                    continue
                if not (val.get("urlPath") or val.get("price") or val.get("priceDetailed")):
                    continue
                try:
                    items.append(_parse_api_item(val))
                except Exception:
                    pass
            if items:
                return items

    # Recurse into dict values
    for key, val in data.items():
        if isinstance(val, dict) and len(str(val)) > 1000:
            result = _extract_items_from_json(val)
            if result:
                return result
    return []


def _parse_api_item(val: dict) -> AvitoItem:
    avito_id = str(val.get("id") or val.get("itemId") or "")
    title = val.get("title") or ""

    # Price
    price_info = val.get("priceDetailed") or val.get("price") or {}
    price_value = None
    price_str = "Цена не указана"
    if isinstance(price_info, dict):
        price_value = price_info.get("value")
        price_str = price_info.get("string") or ""
        if not price_str and price_value:
            price_str = f"{int(price_value):,} ₽".replace(",", " ")
    elif isinstance(price_info, (int, float)):
        price_value = int(price_info)
        price_str = f"{price_value:,} ₽".replace(",", " ")

    # URL
    url_path = val.get("urlPath") or val.get("url") or ""
    if url_path and not url_path.startswith("http"):
        item_url = f"https://www.avito.ru{url_path}"
    else:
        item_url = url_path or "https://www.avito.ru"

    # Image
    image_url = None
    images = val.get("images") or []
    if images and isinstance(images[0], dict):
        img0 = images[0]
        image_url = (
            img0.get("864x648")
            or img0.get("636x476")
            or img0.get("432x324")
            or img0.get("url")
        )
        # Sometimes image is a dict of size->url under "variants"
        if not image_url and isinstance(img0.get("variants"), dict):
            variants = img0["variants"]
            image_url = (
                variants.get("864x648")
                or variants.get("636x476")
                or next(iter(variants.values()), None)
            )

    # Location
    location = None
    loc = val.get("location") or {}
    if isinstance(loc, dict):
        location = loc.get("name")

    # Description
    desc = val.get("description") or ""
    if isinstance(desc, dict):
        desc = desc.get("text") or ""
    if desc and len(desc) > 300:
        desc = desc[:300] + "..."

    # Seller
    seller = val.get("seller") or {}
    seller_name = seller.get("name") if isinstance(seller, dict) else None

    # Timestamp
    ts_raw = (
        val.get("sortTimeStamp")
        or val.get("time")
        or val.get("publishDate")
        or val.get("sortTime")
    )
    ts: int | None = None
    if isinstance(ts_raw, (int, float)):
        ts_int = int(ts_raw)
        if ts_int > 1_000_000_000_000:  # ms
            ts = ts_int // 1000
        else:
            ts = ts_int

    return AvitoItem(
        avito_id=avito_id,
        title=title,
        price=price_str,
        price_value=int(price_value) if isinstance(price_value, (int, float)) else None,
        url=item_url,
        image_url=image_url,
        location=location,
        description=desc or None,
        seller_name=seller_name,
        published_timestamp=ts,
    )
