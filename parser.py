"""Avito parser — primary: mobile API, fallback: HTML hydration JSON."""
import base64
import html as html_lib
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

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
    """Fetch via the Avito mobile API. Passes the user's URL path so location and
    category are honored (not hardcoded). Returns (items, blocked)."""
    import asyncio
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    # Use the search endpoint that accepts a path/slug — this respects the
    # city and category from the URL (e.g. /krasnodar/telefony) instead of
    # falling back to a hardcoded location.
    api_url = "https://m.avito.ru/api/9/items"
    params = {
        "key": config.avito_api_key,
        "limit": "50",
        "display": "list",
        # forceLocation prevents Avito from "expanding" the search to nearby cities
        "forceLocation": "true",
        # s=104 = sort by date desc; carry user's sort if present
        "sort": "date",
    }

    # Pass the path so the API knows the city + category from the URL
    if parsed.path and parsed.path != "/":
        params["url"] = parsed.path

    # Forward all known filter params so the API receives the same intent as the
    # web URL. Avito's API accepts most of these directly.
    forward_keys = {
        "f": "f",
        "q": "query",
        "pmin": "priceMin",
        "pmax": "priceMax",
        "user": "user",
        "bt": "bt",
        "cd": "cd",
        "s": "sort",
        "localPriority": "localPriority",
        "radius": "radius",
    }
    for src, dst in forward_keys.items():
        if src in qs and qs[src]:
            params[dst] = qs[src][0]

    # If the user URL has s=104 (sort by date), keep it as numeric;
    # otherwise enforce date sorting.
    if "s" in qs and qs["s"]:
        params["sort"] = qs["s"][0]

    def _do_request():
        full_url = api_url + "?" + urlencode(params, doseq=False)
        try:
            scraper = _get_cloudscraper(proxy)
            proxies = {"http": proxy, "https": proxy} if proxy else None
            resp = scraper.get(
                full_url,
                proxies=proxies,
                headers={
                    "Accept": "application/json",
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Referer": url,
                    "Origin": "https://m.avito.ru",
                    "X-Requested-With": "XMLHttpRequest",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin",
                    "Cache-Control": "no-cache",
                },
                timeout=30,
            )
            return resp.status_code, resp.text
        except Exception as e:
            logger.debug("[api] cloudscraper error: %s", e)
            return 0, ""

    try:
        loop = asyncio.get_running_loop()
        status, text = await loop.run_in_executor(None, _do_request)

        if status in (429, 403):
            logger.warning("[api] BLOCKED %d for %s", status, url[:80])
            return None, True
        if status != 200:
            logger.debug("[api] HTTP %d for %s", status, url[:80])
            return None, False

        data = orjson.loads(text)
        items_raw = (data.get("result") or {}).get("items") or []
        items: list[AvitoItem] = []
        skipped = 0
        for raw in items_raw:
            if not _is_real_listing(raw):
                skipped += 1
                continue
            val = raw.get("value") or {}
            try:
                items.append(_parse_api_item(val))
            except Exception as e:
                logger.debug("parse item err: %s", e)
        if items:
            logger.info(
                "[api] SUCCESS: %d items from mobile API (skipped %d promoted/non-item)",
                len(items), skipped,
            )
        return (items if items else None), False
    except Exception as e:
        logger.debug("[api] exception: %s", e)
        return None, False


# Promoted/VIP item types and flags that should never be considered as
# "results" for a filtered search.
_NON_LISTING_TYPES = {
    "xlItem", "vipItem", "vip", "topAdvertisement", "topAdvert", "topAd",
    "premium", "promoted", "advertising", "ad", "banner", "snippet",
    "recommendation", "recommendations", "similar", "alternative",
}


def _is_real_listing(raw: dict) -> bool:
    """Return True only for normal search results — not promoted/VIP/recommended."""
    if not isinstance(raw, dict):
        return False
    rtype = (raw.get("type") or "").strip()
    if rtype and rtype != "item":
        return False
    val = raw.get("value") if isinstance(raw.get("value"), dict) else raw
    if not isinstance(val, dict):
        return False
    # Avito flags promoted/highlighted items
    for flag in ("isPromoted", "isHighlighted", "isVip", "isPremium",
                 "isAdvertising", "promoted", "vip", "premium"):
        if val.get(flag):
            return False
    # Some payloads put the same info under "settings" or "highlight"
    settings = val.get("settings") or {}
    if isinstance(settings, dict):
        if settings.get("isHighlighted") or settings.get("isPromoted"):
            return False
    return True


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
    # Warmup: visit BOTH domains for cookies
    try:
        r = s.get("https://www.avito.ru/", proxies=proxies, timeout=20)
        logger.info("[html] warmup www: HTTP %d, %d cookies", r.status_code, len(s.cookies))
    except Exception as e:
        logger.warning("[html] warmup www failed: %s", e)
    try:
        r2 = s.get("https://m.avito.ru/", proxies=proxies, timeout=20)
        logger.info("[html] warmup m: HTTP %d, %d cookies", r2.status_code, len(s.cookies))
    except Exception as e:
        logger.warning("[html] warmup m failed: %s", e)
    _cs_session = s
    _cs_created = now
    return s


def extract_price_range(url: str) -> tuple[int | None, int | None]:
    """Pull (min_price, max_price) out of an Avito URL.

    Checks, in order:
      1. pmin/pmax query params (explicit and most reliable)
      2. price_min / price_max (older variant)
      3. The JSON blob inside the f= base64 parameter, which holds
         filter state like {"<key>": {"from": 1000, "to": 5000}}

    Returns (None, None) if no range is found."""
    try:
        parsed = urlparse(url)
    except Exception:
        return None, None

    qs = parse_qs(parsed.query, keep_blank_values=True)

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    pmin = _int((qs.get("pmin") or qs.get("price_min") or [None])[0])
    pmax = _int((qs.get("pmax") or qs.get("price_max") or [None])[0])

    if pmin is not None or pmax is not None:
        return pmin, pmax

    # Try decoding f= — it's URL-safe base64 with a JSON blob holding
    # filter state. The price filter shows up as {"from": N, "to": M}.
    f_val = (qs.get("f") or [None])[0]
    if not f_val:
        return None, None
    try:
        padded = f_val + "=" * (-len(f_val) % 4)
        raw = base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
        # Skip any leading non-JSON bytes
        start = raw.find("{")
        if start < 0:
            return None, None
        data = orjson.loads(raw[start:])
    except Exception:
        return None, None

    # Walk the JSON and collect the first {from, to} pair
    def _walk(node):
        if isinstance(node, dict):
            if "from" in node or "to" in node:
                return _int(node.get("from")), _int(node.get("to"))
            for v in node.values():
                r = _walk(v)
                if r != (None, None):
                    return r
        elif isinstance(node, list):
            for v in node:
                r = _walk(v)
                if r != (None, None):
                    return r
        return None, None

    return _walk(data)


def _isolate_search_results(html: str) -> str:
    """Cut the HTML down to just the main search results container.
    Avito puts 'recommended' / 'similar' listings AFTER the main serp;
    if we let regex see the whole page we get items from those blocks too."""
    # Try the explicit serp marker first
    start_markers = [
        'data-marker="catalog-serp"',
        'data-marker="catalog-list"',
        'class="items-items',
    ]
    end_markers = [
        'data-marker="recommendations',
        'data-marker="rec-items',
        'data-marker="similar',
        'class="recommendations',
        'class="similar',
    ]

    start = -1
    for m in start_markers:
        i = html.find(m)
        if i >= 0:
            start = i
            break
    if start < 0:
        return html  # fallback — use whole page

    # Find earliest end marker after start
    end = len(html)
    for m in end_markers:
        i = html.find(m, start)
        if 0 < i < end:
            end = i
    return html[start:end]


def _ensure_sort_by_date(url: str) -> str:
    """Make sure the search URL is sorted by date desc (s=104).
    Without this, Avito may return 'recommended' ordering which mixes
    in old/promoted listings.

    IMPORTANT: we must NOT reparse/reencode the URL — doing so would
    re-encode the f= base64 parameter (which contains URL-safe `-` and `_`
    that Python's urlencode will leave alone, but other characters might
    get re-percent-encoded). Just append &s=104 if missing."""
    # Quick check for existing s= parameter
    if re.search(r"[?&]s=\d+", url):
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + "s=104"


def _fetch_html_sync(url: str, proxy: str | None) -> tuple[int, str] | None:
    """Fetch Avito HTML with cloudscraper (sync, runs in thread)."""
    try:
        s = _get_cloudscraper(proxy)
        proxies = {"http": proxy, "https": proxy} if proxy else None
        sorted_url = _ensure_sort_by_date(url)
        resp = s.get(
            sorted_url,
            proxies=proxies,
            timeout=60,
            allow_redirects=False,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        )
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
                    # IMPORTANT: only read from catalog.items — never from
                    # "recommendations", "similar", or other adjacent blocks.
                    catalog = data.get("state", {}).get("data", {}).get("catalog", {})
                    items_raw = catalog.get("items") or []
                    items: list[AvitoItem] = []
                    skipped = 0
                    for raw in items_raw:
                        if not _is_real_listing(raw):
                            skipped += 1
                            continue
                        val = raw.get("value") if isinstance(raw, dict) and "value" in raw else raw
                        if not isinstance(val, dict):
                            continue
                        try:
                            items.append(_parse_api_item(val))
                        except Exception:
                            pass
                    if items:
                        logger.info(
                            "[html] parsed %d items from catalog.items (skipped %d promoted)",
                            len(items), skipped,
                        )
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
    # IMPORTANT: limit the search area to the main search results container
    # so we don't pick up items from "recommended" / "similar" blocks below.
    serp_html = _isolate_search_results(html)
    item_ids = re.findall(r'data-item-id="(\d+)"', serp_html)
    if len(item_ids) >= 3:
        items = []
        seen_ids = set()
        for item_id in item_ids:
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            idx = serp_html.find(f'data-item-id="{item_id}"')
            block = serp_html[idx:idx + 3000] if idx >= 0 else ""
            # Skip blocks marked as VIP/promo
            if 'data-marker="item-vip' in block or 'iva-item-titleStep' in block and 'isPromoted' in block:
                continue
            title_m = re.search(r'title="([^"]{5,80})"', block)
            title = title_m.group(1) if title_m else f"Объявление {item_id}"
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
        logger.info("[html] parsed %d items from data-item-id (in serp area)", len(items))
        return items, False

    logger.warning("[html] no items found in HTML (size=%d)", len(html))
    return None, False


# ---------------------------------------------------------------------------
# Item parsing
# ---------------------------------------------------------------------------

# Keys that are known to contain "recommended"/"similar" listings — never
# read items from these.
_FORBIDDEN_PARENT_KEYS = {
    "recommendations", "recommendation", "recommended",
    "similar", "similarItems", "alternatives", "alternative",
    "youMayLike", "youMayAlsoLike", "alsoLooked",
    "advertising", "promo", "promoted", "banners", "vip",
    "topItems", "topAdverts", "popular",
}


def _extract_items_from_json(data: dict, parent_key: str = "") -> list[AvitoItem]:
    """Find listings in the catalog block ONLY. Never recurse into recommendation
    or 'similar items' blocks — those mix in items that don't match the user's
    filters and would cause false notifications."""
    if not isinstance(data, dict):
        return []

    # Refuse to read items if we are inside a recommendation/similar block.
    if parent_key.lower() in {k.lower() for k in _FORBIDDEN_PARENT_KEYS}:
        return []

    # Only read from explicit catalog paths — never from arbitrary "items".
    for getter in (
        lambda d: d.get("catalog", {}).get("items", []) if isinstance(d.get("catalog"), dict) else [],
        lambda d: (d.get("state", {}).get("data", {}).get("catalog", {}) or {}).get("items", []) if isinstance(d.get("state"), dict) else [],
    ):
        try:
            items_raw = getter(data)
        except Exception:
            items_raw = []
        if isinstance(items_raw, list) and len(items_raw) >= 1:
            items = _items_from_raw_list(items_raw)
            if items:
                return items

    # Recurse only into dict values, but skip forbidden keys
    for key, val in data.items():
        if key.lower() in {k.lower() for k in _FORBIDDEN_PARENT_KEYS}:
            continue
        if isinstance(val, dict) and len(str(val)) > 1000:
            result = _extract_items_from_json(val, parent_key=key)
            if result:
                return result
    return []


_IMAGE_SIZE_KEYS = (
    "864x648", "636x476", "540x405", "432x324", "318x238",
    "main", "big", "biggest", "default", "url",
)


def _extract_image_url(val: dict) -> str | None:
    """Pull the best available image URL from whatever shape Avito used.

    Observed shapes:
      - images: [{"864x648": "...", "636x476": "...", ...}]
      - images: [{"variants": {"864x648": "...", ...}}]
      - images: [{"url": "..."}]
      - imagesAlt: same
      - image: {"864x648": "..."} or {"url": "..."}
      - images: [{"sizes": {"1280x960": "..."}}]
      - cover: {"url": "..."}
    """
    # 1) list-style
    for key in ("images", "imagesAlt", "photos", "gallery"):
        lst = val.get(key)
        if isinstance(lst, list) and lst:
            first = lst[0]
            url = _image_from_dict(first)
            if url:
                return url

    # 2) single-dict style
    for key in ("image", "cover", "mainImage", "thumbnail"):
        obj = val.get(key)
        if isinstance(obj, dict):
            url = _image_from_dict(obj)
            if url:
                return url
        elif isinstance(obj, str) and obj.startswith("http"):
            return obj
    return None


def _image_from_dict(obj) -> str | None:
    if isinstance(obj, str) and obj.startswith("http"):
        return obj
    if not isinstance(obj, dict):
        return None
    # Direct size keys
    for k in _IMAGE_SIZE_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    # Nested variants / sizes
    for nest_key in ("variants", "sizes", "urls"):
        nested = obj.get(nest_key)
        if isinstance(nested, dict):
            for k in _IMAGE_SIZE_KEYS:
                v = nested.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    return v
            # Fall back to any http value inside
            for v in nested.values():
                if isinstance(v, str) and v.startswith("http"):
                    return v
    # Last resort — any http value at the top level
    for v in obj.values():
        if isinstance(v, str) and v.startswith("http") and (
            ".jpg" in v or ".jpeg" in v or ".png" in v or ".webp" in v
        ):
            return v
    return None


def _items_from_raw_list(items_raw: list) -> list[AvitoItem]:
    items: list[AvitoItem] = []
    for raw in items_raw:
        if not _is_real_listing(raw):
            continue
        val = raw.get("value", raw) if isinstance(raw, dict) and "value" in raw else raw
        if not isinstance(val, dict):
            continue
        if not (val.get("id") or val.get("itemId")):
            continue
        if not (val.get("urlPath") or val.get("price") or val.get("priceDetailed")):
            continue
        try:
            items.append(_parse_api_item(val))
        except Exception:
            pass
    return items


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

    # Image — Avito uses several different shapes depending on the endpoint
    image_url = _extract_image_url(val)

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
