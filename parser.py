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


async def download_image_bytes(url: str) -> bytes | None:
    """Download an image through the same cloudscraper session + proxy
    we use for HTML. Telegram can't fetch images from Avito's CDN
    directly (Avito blocks Telegram's IPs), so we act as the fetcher."""
    if not url:
        return None
    proxy = config.proxy_list[0] if config.proxy_list else None

    def _do_download():
        try:
            s = _get_cloudscraper(proxy)
            proxies = {"http": proxy, "https": proxy} if proxy else None
            resp = s.get(
                url,
                proxies=proxies,
                timeout=15,
                headers={"Referer": "https://www.avito.ru/"},
            )
            if resp.status_code != 200:
                logger.debug("[image] HTTP %d for %s", resp.status_code, url[:80])
                return None
            content = resp.content
            if not content or len(content) < 500:
                return None
            # Quick magic-byte sanity check
            if content.startswith((b"\xff\xd8\xff",      # JPEG
                                   b"\x89PNG",            # PNG
                                   b"RIFF",               # WEBP (container)
                                   b"GIF8")):
                return content
            logger.debug("[image] not an image: %s, first bytes=%r",
                         url[:80], content[:8])
            return None
        except Exception as e:
            logger.debug("[image] download err: %s", e)
            return None

    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _do_download)


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
    """Walk every <script data-mfe-state="true">, dump top-level state.data
    keys of each, then find the mfe whose catalog actually applied the URL
    filters (mainCategoryId / totalCount / searchHash present)."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("[html] beautifulsoup4 not installed")
        return None

    soup = BeautifulSoup(html, "html.parser")
    mfe_scripts = soup.select('script[data-mfe-state="true"]')
    logger.info("[html] found %d mfe-state scripts", len(mfe_scripts))

    # First pass — parse each script's state.data and log its top-level
    # keys so we can tell which one actually applied the filter.
    parsed_mfes = []  # list of (idx, state_data_dict)
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
        state_data = (data.get("state") or {}).get("data")
        if not isinstance(state_data, dict):
            logger.info("[html] mfe #%d: no state.data", idx)
            continue
        logger.info("[html] mfe #%d state.data keys: %s",
                    idx, list(state_data.keys())[:30])
        parsed_mfes.append((idx, state_data))

    # Second pass — pick the mfe whose STATE.DATA has filter metadata.
    # Avito puts totalCount/searchHash/filtersV2 at state.data level, NOT
    # inside state.data.catalog. So check state.data first.
    filter_marker_fields = (
        "totalCount", "totalElements", "mainCount", "count",
        "searchHash", "filtersV2", "filtersGroup", "searchCore", "mcId",
    )

    # Candidate catalogs — (idx, catalog_dict, state_data, has_filter_markers)
    candidates = []
    for idx, state_data in parsed_mfes:
        catalog = state_data.get("catalog")
        if not isinstance(catalog, dict):
            continue
        items_raw = catalog.get("items")
        if not isinstance(items_raw, list):
            continue
        has_markers = any(state_data.get(k) not in (None, "") for k in filter_marker_fields)
        logger.info(
            "[html] mfe #%d catalog: %d items, filter-markers=%s (at state.data), catalog.keys=%s",
            idx, len(items_raw), has_markers, list(catalog.keys())[:20],
        )
        if has_markers:
            _dbg = {k: state_data.get(k) for k in filter_marker_fields
                    if state_data.get(k) not in (None, "")}
            logger.info("[html] mfe #%d filter meta: %s", idx, _dbg)
        candidates.append((idx, catalog, state_data, has_markers))

    if not candidates:
        logger.warning("[html] no catalog MFE found")
        return None

    picked = next(((i, c) for i, c, _s, m in candidates if m), None)
    if picked is None:
        logger.warning(
            "[html] no catalog MFE has filter markers — using first candidate"
        )
        idx, catalog = candidates[0][0], candidates[0][1]
    else:
        idx, catalog = picked
        logger.info("[html] picked mfe #%d as the filter-applied catalog", idx)

    items_raw = catalog.get("items") or []

    # Parse into AvitoItems — skip non-item rows (banners / snippets)
    items: list[AvitoItem] = []
    skipped_non_item = 0
    missing_image_sample = None  # (item_id, raw_keys) for one bad item
    missing_location_sample = None
    missing_desc_sample = None
    missing_ts_sample = None
    for raw in items_raw:
        if not isinstance(raw, dict):
            continue
        rtype = (raw.get("type") or "").strip()
        if rtype and rtype not in ("item", ""):
            skipped_non_item += 1
            continue
        val = raw.get("value", raw) if "value" in raw else raw
        if not isinstance(val, dict):
            continue
        if not (val.get("id") or val.get("itemId")):
            continue
        try:
            parsed = _parse_api_item(val)
            items.append(parsed)
            # Capture one sample per missing field — enough to know the
            # catalog shape without flooding logs.
            if parsed.image_url is None and missing_image_sample is None:
                missing_image_sample = (parsed.avito_id, list(val.keys())[:30])
            if parsed.location is None and missing_location_sample is None:
                missing_location_sample = (parsed.avito_id, list(val.keys())[:30])
            if parsed.description is None and missing_desc_sample is None:
                missing_desc_sample = (parsed.avito_id, list(val.keys())[:30])
            if parsed.published_timestamp is None and missing_ts_sample is None:
                missing_ts_sample = (parsed.avito_id, list(val.keys())[:30])
        except Exception:
            pass

    logger.info(
        "[html] mfe #%d is CATALOG: %d raw rows -> %d items (%d non-item rows skipped)",
        idx, len(items_raw), len(items), skipped_non_item,
    )

    # Completeness stats — how many items have each field
    if items:
        total = len(items)
        with_image = sum(1 for i in items if i.image_url)
        with_loc = sum(1 for i in items if i.location)
        with_desc = sum(1 for i in items if i.description)
        with_ts = sum(1 for i in items if i.published_timestamp)
        logger.info(
            "[html] completeness: image=%d/%d, location=%d/%d, desc=%d/%d, date=%d/%d",
            with_image, total, with_loc, total, with_desc, total, with_ts, total,
        )
        if missing_image_sample:
            logger.info("[html] MISSING image: item=%s, val.keys=%s",
                        *missing_image_sample)
        if missing_location_sample:
            logger.info("[html] MISSING location: item=%s, val.keys=%s",
                        *missing_location_sample)
        if missing_desc_sample:
            logger.info("[html] MISSING description: item=%s, val.keys=%s",
                        *missing_desc_sample)
        if missing_ts_sample:
            logger.info("[html] MISSING date: item=%s, val.keys=%s",
                        *missing_ts_sample)

        sample_paths = [i.url.replace("https://www.avito.ru", "")[:60] for i in items[:3]]
        logger.info("[html] sample item paths: %s", sample_paths)
    return items


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

    # Image — Avito has used many shapes over time. Walk everything
    # that looks like an image dict and pick the biggest http URL.
    image_url = _extract_image_url(val)

    # Location — Avito uses several shapes across endpoints:
    #   location: {"name": "Москва"}                   (old API)
    #   location: {"id": N, "slug": "moskva"}          (new — no name here)
    #   geo: {"formattedAddress": "Москва, метро..."}  (new primary)
    #   addressDetailed: {"text": "...", "name": "..."}
    #   geoReferences: [{"content": "Москва"}]
    location = _extract_location(val)

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


# ---------------------------------------------------------------------------
# Image extraction — Avito uses different keys per endpoint
# ---------------------------------------------------------------------------

# Strict: ONLY explicit size keys. Not "url" / "main" — those can point
# to share/item pages, not image CDN, and Telegram chokes on non-images.
_IMAGE_SIZE_KEYS = (
    # Square crops — Avito's modern default
    "864x864", "636x636", "540x540", "472x472", "432x432",
    # Rectangular legacy
    "864x648", "636x476", "540x405", "432x324", "318x238", "208x208",
    "140x105", "72x54",
)

# Hostnames that Avito actually serves images from. Reject anything else
# so we don't hand Telegram an avito.ru/item/12345 (HTML) and get a
# "wrong type of the web page content" error.
_IMAGE_HOST_HINTS = ("avito.st", "avito.ru/images", "avatars.mds.yandex", "80.img.avito.st")


def _looks_like_image_url(u: str) -> bool:
    if not isinstance(u, str) or not u.startswith("http"):
        return False
    low = u.lower()
    # Extension hint
    if any(ext in low for ext in (".jpg", ".jpeg", ".png", ".webp", ".avif")):
        return True
    # Avito CDN hostnames
    if any(h in low for h in _IMAGE_HOST_HINTS):
        return True
    return False


def _extract_image_url(val: dict) -> str | None:
    """Walk the payload for an image URL that Telegram will actually accept."""
    for key in ("images", "imagesAlt", "photos", "gallery"):
        lst = val.get(key)
        if isinstance(lst, list) and lst:
            url = _image_from_dict(lst[0])
            if url:
                return url
    for key in ("image", "cover", "mainImage", "thumbnail"):
        obj = val.get(key)
        if isinstance(obj, dict):
            url = _image_from_dict(obj)
            if url:
                return url
        elif isinstance(obj, str) and _looks_like_image_url(obj):
            return obj
    return None


def _image_from_dict(obj) -> str | None:
    if isinstance(obj, str):
        return obj if _looks_like_image_url(obj) else None
    if not isinstance(obj, dict):
        return None
    # Explicit size keys first (preferring larger)
    for k in _IMAGE_SIZE_KEYS:
        v = obj.get(k)
        if _looks_like_image_url(v):
            return v
    # Nested "variants" / "sizes" / "urls" dicts
    for nest_key in ("variants", "sizes", "urls"):
        nested = obj.get(nest_key)
        if isinstance(nested, dict):
            for k in _IMAGE_SIZE_KEYS:
                v = nested.get(k)
                if _looks_like_image_url(v):
                    return v
            for v in nested.values():
                if _looks_like_image_url(v):
                    return v
    # Any field value that looks like an image URL
    for v in obj.values():
        if _looks_like_image_url(v):
            return v
    return None


_CITY_SLUG_MAP = {
    # Million+
    "moskva": "Москва", "sankt-peterburg": "Санкт-Петербург",
    "novosibirsk": "Новосибирск", "ekaterinburg": "Екатеринбург",
    "nizhniy_novgorod": "Нижний Новгород", "kazan": "Казань",
    "chelyabinsk": "Челябинск", "omsk": "Омск", "samara": "Самара",
    "rostov-na-donu": "Ростов-на-Дону", "ufa": "Уфа",
    "krasnoyarsk": "Красноярск", "perm": "Пермь", "voronezh": "Воронеж",
    "volgograd": "Волгоград", "krasnodar": "Краснодар",
    # 500k+
    "saratov": "Саратов", "tyumen": "Тюмень", "tolyatti": "Тольятти",
    "izhevsk": "Ижевск", "barnaul": "Барнаул", "ulyanovsk": "Ульяновск",
    "irkutsk": "Иркутск", "khabarovsk": "Хабаровск",
    "vladivostok": "Владивосток", "yaroslavl": "Ярославль",
    "makhachkala": "Махачкала", "tomsk": "Томск", "orenburg": "Оренбург",
    "kemerovo": "Кемерово", "novokuznetsk": "Новокузнецк",
    "ryazan": "Рязань", "astrakhan": "Астрахань", "penza": "Пенза",
    "naberezhnye_chelny": "Набережные Челны", "lipetsk": "Липецк",
    "kirov": "Киров", "cheboksary": "Чебоксары", "tula": "Тула",
    "kaliningrad": "Калининград", "balashikha": "Балашиха",
    "kursk": "Курск", "sevastopol": "Севастополь",
    "sochi": "Сочи", "stavropol": "Ставрополь", "ulan-ude": "Улан-Удэ",
    "tver": "Тверь", "magnitogorsk": "Магнитогорск", "ivanovo": "Иваново",
    "bryansk": "Брянск", "simferopol": "Симферополь",
    # 250k+
    "belgorod": "Белгород", "surgut": "Сургут", "vladimir": "Владимир",
    "nizhniy_tagil": "Нижний Тагил", "arkhangelsk": "Архангельск",
    "chita": "Чита", "groznyy": "Грозный", "kaluga": "Калуга",
    "smolensk": "Смоленск", "yakutsk": "Якутск", "sterlitamak": "Стерлитамак",
    "volzhskiy": "Волжский", "saransk": "Саранск", "podolsk": "Подольск",
    "kurgan": "Курган", "cherepovets": "Череповец", "oryol": "Орёл",
    "orel": "Орёл", "vologda": "Вологда", "kostroma": "Кострома",
    "tambov": "Тамбов", "pskov": "Псков", "murmansk": "Мурманск",
    "taganrog": "Таганрог", "komsomolsk-na-amure": "Комсомольск-на-Амуре",
    "nizhnevartovsk": "Нижневартовск", "petrozavodsk": "Петрозаводск",
    "yoshkar-ola": "Йошкар-Ола", "syktyvkar": "Сыктывкар",
    "khimki": "Химки", "mytishchi": "Мытищи", "lyubertsy": "Люберцы",
    "krasnogorsk": "Красногорск", "korolev": "Королёв",
    "engels": "Энгельс", "nakhodka": "Находка", "blagoveshchensk": "Благовещенск",
    "stavropolskiy": "Ставропольский край",
    "novorossiysk": "Новороссийск", "pyatigorsk": "Пятигорск",
    "maykop": "Майкоп", "novyy_urengoy": "Новый Уренгой",
    "noyabrsk": "Ноябрьск", "nefteyugansk": "Нефтеюганск",
    "derbent": "Дербент", "nalchik": "Нальчик",
    "vladikavkaz": "Владикавказ", "cherkessk": "Черкесск",
    "magadan": "Магадан", "yuzhno-sakhalinsk": "Южно-Сахалинск",
    "petropavlovsk-kamchatskiy": "Петропавловск-Камчатский",
    "barnaulskiy": "Барнаульский",
    # 100k+
    "podolskiy": "Подольский", "dzerzhinsk": "Дзержинск",
    "zheleznodorozhnyy": "Железнодорожный",
    "staryy_oskol": "Старый Оскол", "elektrostal": "Электросталь",
    "murom": "Муром", "novocherkassk": "Новочеркасск",
    "balakovo": "Балаково", "abakan": "Абакан",
    "armavir": "Армавир", "batayk": "Батайск", "batayskiy": "Батайск",
    "elista": "Элиста", "essentuki": "Ессентуки",
    "kislovodsk": "Кисловодск", "mineralnye_vody": "Минеральные Воды",
    "nevinnomyssk": "Невинномысск", "novokuybyshevsk": "Новокуйбышевск",
    "salavat": "Салават", "neftekamsk": "Нефтекамск",
    "arzamas": "Арзамас", "dubna": "Дубна", "serpukhov": "Серпухов",
    "orekhovo-zuevo": "Орехово-Зуево", "ramenskoye": "Раменское",
    "schelkovo": "Щёлково", "zhukovskiy": "Жуковский",
    "noginsk": "Ногинск", "sergiev_posad": "Сергиев Посад",
    "pavlovskiy_posad": "Павловский Посад", "klin": "Клин",
    "reutov": "Реутов", "voskresensk": "Воскресенск",
    "lobnya": "Лобня", "solnechnogorsk": "Солнечногорск",
    "nizhnekamsk": "Нижнекамск", "almetyevsk": "Альметьевск",
    "zelenodolsk": "Зеленодольск", "bugulma": "Бугульма",
    "ozerskiy": "Озёрск", "kopeisk": "Копейск", "zlatoust": "Златоуст",
    "miass": "Миасс", "tobolsk": "Тобольск", "tagil": "Тагил",
    "kamensk-uralskiy": "Каменск-Уральский", "serov": "Серов",
    "pervouralsk": "Первоуральск", "berezniki": "Березники",
    "solikamsk": "Соликамск", "kungur": "Кунгур",
    "votkinsk": "Воткинск", "sarapul": "Сарапул",
    "cheboksarskiy": "Чебоксарский", "alatyr": "Алатырь",
    "yurga": "Юрга", "belovo": "Белово", "leninsk-kuznetskiy": "Ленинск-Кузнецкий",
    "prokopyevsk": "Прокопьевск", "mezhdurechensk": "Междуреченск",
    "kiselevsk": "Киселёвск", "seversk": "Северск",
    "strezhevoy": "Стрежевой", "novyy_oskol": "Новый Оскол",
    "gubkin": "Губкин", "zheleznogorsk": "Железногорск",
    "shchekino": "Щёкино", "novomoskovsk": "Новомосковск",
    "rzhev": "Ржев", "torzhok": "Торжок", "kimry": "Кимры",
    "borisoglebsk": "Борисоглебск", "rossosh": "Россошь",
    "liski": "Лиски", "yelec": "Елец", "elets": "Елец",
    "usman": "Усмань", "gryazi": "Грязи", "dankov": "Данков",
    "sokol": "Сокол", "belozersk": "Белозерск",
    "velikiy_ustyug": "Великий Устюг", "totma": "Тотьма",
    "ust-kut": "Усть-Кут", "angarsk": "Ангарск",
    "bratsk": "Братск", "usolye-sibirskoe": "Усолье-Сибирское",
    "cheremkhovo": "Черемхово", "zima": "Зима", "nizhneudinsk": "Нижнеудинск",
    "kirovo-chepeck": "Кирово-Чепецк", "slobodskoy": "Слободской",
    "vyatskie_polyany": "Вятские Поляны", "omutninsk": "Омутнинск",
    "kotlas": "Котлас", "severodvinsk": "Северодвинск",
    "novodvinsk": "Новодвинск", "koryazhma": "Коряжма",
    "velikiy_novgorod": "Великий Новгород", "borovichi": "Боровичи",
    "starayа_russa": "Старая Русса", "starayarussa": "Старая Русса",
    "staraya_russa": "Старая Русса",
    "vyborg": "Выборг", "gatchina": "Гатчина", "vsevolozhsk": "Всеволожск",
    "tikhvin": "Тихвин", "sosnovyy_bor": "Сосновый Бор",
    "kirishi": "Кириши", "luga": "Луга", "kingisepp": "Кингисепп",
    "slantsy": "Сланцы",
    "kovrov": "Ковров", "murom": "Муром", "aleksandrov": "Александров",
    "kinzburg": "Кинешма", "kineshma": "Кинешма",
    "shuya": "Шуя", "furmanov": "Фурманов",
    "yuzha": "Южа",
    "feodosiya": "Феодосия", "kerch": "Керчь", "yalta": "Ялта",
    "evpatoriya": "Евпатория", "dzhankoy": "Джанкой",
    "saki": "Саки", "bakhchisaray": "Бахчисарай",
    "gelendzhik": "Геленджик", "anapa": "Анапа", "tuapse": "Туапсе",
    "kropotkin": "Кропоткин", "slavyansk-na-kubani": "Славянск-на-Кубани",
    "temryuk": "Темрюк", "yeysk": "Ейск", "labinsk": "Лабинск",
    "tikhoretsk": "Тихорецк", "armavirskiy": "Армавирский",
    "belorechensk": "Белореченск", "kurganinsk": "Курганинск",
    "apsheronsk": "Апшеронск", "primorsko-akhtarsk": "Приморско-Ахтарск",
    "novokubansk": "Новокубанск", "ust-labinsk": "Усть-Лабинск",
    "goryachiy_klyuch": "Горячий Ключ", "krymsk": "Крымск",
    "abinsk": "Абинск",
    "chaykovskiy": "Чайковский", "krasnokamsk": "Краснокамск",
    "dimitrovgrad": "Димитровград", "syzran": "Сызрань",
    "novokuybyshevsk": "Новокуйбышевск", "chapaevsk": "Чапаевск",
    "otradnyy": "Отрадный", "kinel": "Кинель",
    "novyy_urengoyskiy": "Новый Уренгой",
    "gubkinskiy": "Губкинский", "muravlenko": "Муравленко",
    "labytnangi": "Лабытнанги", "nadym": "Надым",
    "salekhard": "Салехард", "kogalym": "Когалым",
    "pyt-yakh": "Пыть-Ях", "megion": "Мегион",
    "raduzhnyy": "Радужный", "langepas": "Лангепас",
    "lyantor": "Лянтор", "uray": "Урай",
    "yugorsk": "Югорск", "sovetskiy": "Советский",
    "belojarsk": "Белоярский", "nyagan": "Нягань",
    "pyatigorskiy": "Пятигорский",
    "essentukiy": "Ессентуки",
    "budennovsk": "Буденновск", "georgievsk": "Георгиевск",
    "izobilnyy": "Изобильный", "lermontov": "Лермонтов",
    "mikhaylovsk": "Михайловск", "neftekumsk": "Нефтекумск",
    "kazan_city": "Казань",
    "kyzyl": "Кызыл",
    "gorno-altaysk": "Горно-Алтайск",
    "anadyr": "Анадырь", "birobidzhan": "Биробиджан",
    "kaspiysk": "Каспийск", "buynaksk": "Буйнакск",
    "derbent_city": "Дербент", "khasavyurt": "Хасавюрт",
    "kizlyar": "Кизляр", "izberbash": "Избербаш",
    "karachaevsk": "Карачаевск", "magas": "Магас",
    "nazran": "Назрань", "malgobek": "Малгобек",
    # ХМАО / ЯНАО / Якутия
    "khanty-mansiysk": "Ханты-Мансийск",
    "neryungri": "Нерюнгри", "mirnyy": "Мирный",
    "aldanskiy": "Алдан", "aldan": "Алдан",
    "lensk": "Ленск", "udachnyy": "Удачный",
    # Мосрентген / Москва микрорайоны
    "mosrentgen": "Мосрентген", "zelenograd": "Зеленоград",
    "troitsk": "Троицк",
    # Новые (с Avito)
    "alikovo": "Аликово",
    "ozerskoye": "Озёрское", "odintsovo": "Одинцово",
    "pushkino": "Пушкино", "ivanteyevka": "Ивантеевка",
    "fryazino": "Фрязино", "dolgoprudnyy": "Долгопрудный",
    "dmitrov": "Дмитров", "yegoryevsk": "Егорьевск",
    "kolomna": "Коломна", "zaraysk": "Зарайск",
    "kashira": "Кашира", "stupino": "Ступино",
    "chekhov": "Чехов", "naro-fominsk": "Наро-Фоминск",
    "shatura": "Шатура", "pavlovskiy-posad": "Павловский Посад",
    "obninsk": "Обнинск", "kaluzhskiy": "Калужский",
    "ryazanskiy": "Рязанский",
    "ufimskiy": "Уфимский", "blagoveshchenskiy": "Благовещенский",
    "belorechenskiy": "Белореченский",
    "kerchenskiy": "Керченский",
    "kolpino": "Колпино", "pushkin": "Пушкин",
    "petergof": "Петергоф", "lomonosov": "Ломоносов",
    "kronshtadt": "Кронштадт", "sestroretsk": "Сестрорецк",
    "zelenogorsk": "Зеленогорск",
}


def _city_from_url_path(url_path: str) -> str | None:
    """Extract city slug from urlPath like /moskva/odezhda_.../item and
    convert to human name. First segment is the city slug on every Avito
    item URL."""
    if not url_path:
        return None
    parts = url_path.lstrip("/").split("/", 1)
    if not parts or not parts[0]:
        return None
    slug = parts[0].lower()
    if slug == "all":
        return None  # country-wide URL — no city
    if slug in _CITY_SLUG_MAP:
        return _CITY_SLUG_MAP[slug]
    # Unknown slug — convert kebab/snake to Title Case
    pretty = slug.replace("-", " ").replace("_", " ").strip()
    if pretty:
        return pretty[:1].upper() + pretty[1:]
    return None


def _extract_location(val: dict) -> str | None:
    """Try every known shape Avito uses for the location string, falling
    back to the city slug from urlPath which is ALWAYS present."""
    # Direct location dict
    loc = val.get("location")
    if isinstance(loc, dict):
        for k in ("name", "namePrepositional", "nameLocative",
                  "formattedAddress", "text"):
            v = loc.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    elif isinstance(loc, str) and loc.strip():
        return loc.strip()

    # geo block
    geo = val.get("geo")
    if isinstance(geo, dict):
        for k in ("formattedAddress", "address", "name", "text"):
            v = geo.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        refs = geo.get("geoReferences")
        if isinstance(refs, list):
            parts = [r.get("content") for r in refs
                     if isinstance(r, dict) and r.get("content")]
            if parts:
                return ", ".join(parts)

    # addressDetailed
    addr = val.get("addressDetailed")
    if isinstance(addr, dict):
        for k in ("text", "name", "address", "formatted"):
            v = addr.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    elif isinstance(addr, str) and addr.strip():
        return addr.strip()

    # Last resort — first segment of urlPath ("/moskva/...")
    return _city_from_url_path(val.get("urlPath") or "")
