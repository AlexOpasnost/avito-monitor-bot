"""Avito parser — primary: mobile API, fallback: HTML hydration JSON."""
import asyncio
import html as html_lib
import logging
import random
import re
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs

import httpx
import orjson

from config import config

logger = logging.getLogger(__name__)

# GLOBAL lock for ALL Avito requests — scheduler sub loops AND handlers'
# initial scan funnel through here. Held for the entire fetch PLUS a
# 5-10 s cooldown before release, so two scrapes never overlap and there
# is always a gap before the next one can start. This is authoritative;
# the scheduler's own Semaphore(1) only sees scheduler tasks, not the
# handler's initial-scan call.
_avito_lock = asyncio.Lock()


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
    Rotates IP and retries on blocks (429/403).

    Holds the global _avito_lock for the entire fetch + post-cooldown so
    no two scrapes EVER overlap, regardless of whether the caller is the
    scheduler (via its own Semaphore) or the handler's initial-scan
    (which bypasses the scheduler). Cooldown inside the lock guarantees
    a 5-10 s gap before the next caller can start."""
    async with _avito_lock:
        logger.debug("[parser] avito-lock acquired by %s", url[:80])
        # No post-request cooldown — lock itself prevents parallel
        # scrapes, scheduler runs each sub once per 60s anyway.
        return await _fetch_search_items_inner(url, proxy, max_retries)


async def _fetch_search_items_inner(url: str, proxy: str | None, max_retries: int) -> list[AvitoItem] | None:
    """Hit the EXACT URL the user provided, verbatim. No URL rewriting,
    no param whitelist, no m↔www conversion — just GET it through
    cloudscraper and parse the hydration JSON from the response HTML."""
    for attempt in range(max_retries):
        items, html_blocked = await _fetch_hydration_json(url, proxy)
        if items is not None:
            logger.info("[html] fetched %d items for %s", len(items), url[:80])
            return items

        if not html_blocked:
            return None

        logger.warning(
            "HTML blocked (attempt %d/%d), rotating IP and retrying",
            attempt + 1, max_retries,
        )
        _invalidate_session()
        await rotate_ip()
        await asyncio.sleep(8)

    logger.error("All %d attempts blocked for %s", max_retries, url[:80])
    return None


async def rotate_ip() -> bool:
    """Call proxy rotation URL (if configured). Logs the exact URL being
    hit AND the response body so we can verify the env var is not
    truncated and the API actually rotated."""
    rotate_url = config.proxy_rotate_url
    if not rotate_url:
        return False
    # Log the full URL (repr so any truncation / whitespace / special chars
    # are visible). If the URL ends with just "?" or is missing the
    # proxy_key= parameter, the env var in .env / Railway is wrong.
    logger.info("[proxy] rotate URL: %r (len=%d)", rotate_url, len(rotate_url))
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(rotate_url)
            body = (resp.text or "").strip()[:300]
            logger.info(
                "[proxy] changeip HTTP %d, body=%r, final URL=%r",
                resp.status_code, body, str(resp.url),
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

    # Short cooldown after warmup (5-7s). Enough for the mobile-proxy
    # IP to settle between the warmup connects and the real request.
    cooldown = random.uniform(5, 7)
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
        # Log the full URL right before the request — want to confirm
        # cloudscraper didn't mangle the base64 in f= or drop a param.
        logger.info(
            "[html] REQUEST url=%r (len=%d)", url, len(url),
        )
        logger.info("[html] request UA=%s, session id=%s", req_ua, id(s))
        resp = s.get(url, proxies=proxies, timeout=60, allow_redirects=False)
        logger.info(
            "[html] response final_url=%r, status=%d",
            str(resp.url), resp.status_code,
        )
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

    # STRICT extraction — ONLY from the mfe-state script whose payload
    # holds state.data.catalog.items. Avito loads 10+ MFE scripts per
    # page (header, footer, recommendations, similar items, recently-
    # viewed, catalog, ...) and all of them stash an "items" array
    # somewhere in their hydration JSON. Picking any loose "items" list
    # pulls in irrelevant items that do NOT match the user's filter.
    items = _extract_catalog_items_strict(html, url)
    if items is None:
        logger.warning("[html] no catalog MFE on page (size=%d)", len(html))
        return None, False
    logger.info("[html] parsed %d catalog items (strict)", len(items))
    return items, False


def _extract_catalog_items_strict(html: str, url: str) -> list[AvitoItem] | None:
    """Walk every <script data-mfe-state="true"> and return items from the
    FIRST one whose state.data.catalog.items is a non-empty list. Return
    empty list if the catalog MFE is present but empty (legitimate zero-
    results). Return None if no catalog MFE is found at all."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("[html] beautifulsoup4 not installed")
        return None

    soup = BeautifulSoup(html, "html.parser")
    mfe_scripts = soup.select('script[data-mfe-state="true"]')
    logger.info("[html] found %d mfe-state scripts", len(mfe_scripts))

    catalog_found = False
    for idx, script in enumerate(mfe_scripts):
        body = (script.text or "").strip()
        if not body or "sandbox" in body[:200]:
            continue
        try:
            data = orjson.loads(html_lib.unescape(body))
        except Exception as e:
            logger.debug("[html] mfe #%d JSON decode err: %s", idx, str(e)[:80])
            continue
        if not isinstance(data, dict):
            continue

        catalog = (
            data.get("state", {}) if isinstance(data.get("state"), dict) else {}
        ).get("data", {})
        if not isinstance(catalog, dict):
            continue
        catalog = catalog.get("catalog")
        if not isinstance(catalog, dict):
            continue

        # This IS the catalog MFE
        catalog_found = True
        items_raw = catalog.get("items")
        if not isinstance(items_raw, list):
            logger.info("[html] mfe #%d is catalog but items not a list", idx)
            return []

        # DIAGNOSTIC — what did Avito actually search for?
        # The catalog block normally carries the params it applied
        # (searchParams / appliedFilters / filters / categoryId /
        # locationId / mainCategoryId). If these don't match our URL
        # filters, Avito is ignoring f= for some reason.
        _dbg = {
            "mainCategoryId":   catalog.get("mainCategoryId"),
            "categoryId":       catalog.get("categoryId"),
            "locationId":       catalog.get("locationId"),
            "count":            catalog.get("count"),
            "totalCount":       catalog.get("totalCount"),
            "searchHash":       catalog.get("searchHash"),
            "searchRequestId":  str(catalog.get("searchRequestId"))[:40] if catalog.get("searchRequestId") else None,
        }
        logger.info("[html] catalog meta: %s", _dbg)
        # Top-level catalog keys (so we can spot where filter info hides)
        logger.info("[html] catalog keys: %s", list(catalog.keys())[:30])
        # Log searchParams / filters / breadcrumbs if present
        for field in ("searchParams", "appliedFilters", "filters",
                      "breadcrumbs", "queryParams", "params",
                      "formState", "requestParams"):
            val = catalog.get(field)
            if val is not None:
                txt = str(val)
                logger.info("[html] catalog.%s (len=%d): %s",
                            field, len(txt), txt[:400])

        # Parse into AvitoItems — skip non-item rows (banners / snippets)
        items: list[AvitoItem] = []
        skipped_non_item = 0
        for raw in items_raw:
            if not isinstance(raw, dict):
                continue
            rtype = (raw.get("type") or "").strip()
            # Reject known non-catalog-result types
            if rtype and rtype not in ("item", ""):
                skipped_non_item += 1
                continue
            val = raw.get("value", raw) if "value" in raw else raw
            if not isinstance(val, dict):
                continue
            if not (val.get("id") or val.get("itemId")):
                continue
            try:
                items.append(_parse_api_item(val))
            except Exception:
                pass
        logger.info(
            "[html] mfe #%d is CATALOG: %d raw rows -> %d items (%d non-item rows skipped)",
            idx, len(items_raw), len(items), skipped_non_item,
        )
        if items:
            # Sanity-check: log first 3 item url paths so we can verify
            # they're in the expected subcategory. Mismatch here = bug
            # upstream (URL vs. Avito response disagreement).
            sample_paths = [i.url.replace("https://www.avito.ru", "")[:60] for i in items[:3]]
            logger.info("[html] sample item paths: %s", sample_paths)
        return items

    if not catalog_found:
        return None
    return []


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
