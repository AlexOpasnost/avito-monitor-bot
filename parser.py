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
            await asyncio.sleep(3)

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
            await asyncio.sleep(5)

    logger.error("All %d attempts blocked for %s", max_retries, url[:80])
    return None


async def rotate_ip() -> bool:
    """Call proxy rotation URL (if configured)."""
    if not config.proxy_rotate_url:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(config.proxy_rotate_url)
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
    """Fetch via the Avito mobile API. Returns (items, blocked).
    blocked=True means IP is rate-limited/banned."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    params = {
        "key": config.avito_api_key,
        "locationId": "621540",  # Russia default
        "limit": "50",
        "display": "list",
        "sort": "date",
    }

    # Pass canonical filter params through (no path — it breaks the API)
    if "f" in qs:
        params["f"] = qs["f"][0]
    if "q" in qs:
        params["query"] = qs["q"][0]
    if "pmin" in qs:
        params["priceMin"] = qs["pmin"][0]
    if "pmax" in qs:
        params["priceMax"] = qs["pmax"][0]

    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=30, http2=False) as client:
            resp = await client.get(
                "https://m.avito.ru/api/11/items",
                params=params,
                headers={
                    "User-Agent": config.user_agent,
                    "Accept": "application/json",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                    "Referer": "https://m.avito.ru/",
                },
            )
            # Block detection
            if resp.status_code in (429, 403):
                logger.warning("[api] BLOCKED %d for %s", resp.status_code, url[:80])
                return None, True
            if resp.status_code != 200:
                logger.debug("[api] HTTP %d for %s", resp.status_code, url[:80])
                return None, False
            try:
                data = resp.json()
            except Exception:
                return None, False
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
            return (items if items else None), False
    except Exception as e:
        logger.debug("[api] exception: %s", e)
        return None, False


# ---------------------------------------------------------------------------
# HTML fallback (hydration JSON)
# ---------------------------------------------------------------------------

_cs_session = None
_cs_created = 0.0


def _get_cloudscraper(proxy: str | None):
    """Get or create a cloudscraper session (reuse for cookies)."""
    import time
    global _cs_session, _cs_created
    now = time.time()
    if _cs_session and (now - _cs_created) < 300:
        return _cs_session
    import cloudscraper
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True}
    )
    proxies = {"http": proxy, "https": proxy} if proxy else None
    # Warmup: visit main page for cookies
    try:
        r = s.get("https://www.avito.ru/", proxies=proxies, timeout=20)
        logger.info("[html] warmup: HTTP %d, %d cookies", r.status_code, len(s.cookies))
    except Exception as e:
        logger.warning("[html] warmup failed: %s", e)
    _cs_session = s
    _cs_created = now
    return s


def _fetch_html_sync(url: str, proxy: str | None) -> tuple[int, str] | None:
    """Fetch Avito HTML with cloudscraper (sync, runs in thread)."""
    try:
        s = _get_cloudscraper(proxy)
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = s.get(url, proxies=proxies, timeout=60, allow_redirects=False)
        return resp.status_code, resp.text
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
        status, html = resp_data
        if status in (429, 403):
            logger.warning("[html] BLOCKED %d for %s", status, url[:80])
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

    # Method 1: data-mfe-state hydration JSON (modern Avito)
    match = _HYDRATION_RE.search(html)
    if match:
        try:
            raw_json = html_lib.unescape(match.group(1))
            data = orjson.loads(raw_json)
            state = data.get("state") or data
            catalog = (state.get("data") or {}).get("catalog") or state.get("catalog") or {}
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
                logger.info("[html] parsed %d items from mfe-state", len(items))
                return items, False
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

    # Method 3: __preloadedState_ (another JSON variant)
    for var_name in ['__preloadedState_', '__preloadedState__']:
        ps_match = re.search(rf'window\.{var_name}\s*=\s*"(.+?)"\s*;', html, re.DOTALL)
        if ps_match:
            try:
                raw_json = unquote(ps_match.group(1))
                data = orjson.loads(raw_json)
                items = _extract_items_from_json(data)
                if items:
                    logger.info("[html] parsed %d items from %s", len(items), var_name)
                    return items, False
            except Exception:
                pass

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
