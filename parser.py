"""Avito parser — curl-cffi (Chrome TLS impersonation) + HTML hydration parsing.

Architecture rules:
  - ONE persistent AsyncSession across the whole bot lifetime
    (cookies build up like a real browser)
  - impersonate="chrome120" ALWAYS (no rotation — UA/TLS mismatch is a bot
    signal); curl-cffi sets all browser headers — we add NOTHING manually
  - Proxy rotation only when blocked (403 / HTML block), never proactively
  - Smart per-error-type retry behavior with bounded backoff
  - Max 2 attempts per scrape call
"""
import asyncio
import html as html_lib
import logging
import re
from dataclasses import dataclass
from urllib.parse import unquote

import httpx
import orjson
from curl_cffi.requests import AsyncSession

from config import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_IMPERSONATE = "chrome120"
_REQ_TIMEOUT = 45  # seconds — generous for Avito's slow pages
_MAX_ATTEMPTS = 2

# Per-error-type retry waits
_WAIT_AFTER_403 = 10
_WAIT_AFTER_429 = 30
_WAIT_AFTER_TIMEOUT = 15
_WAIT_AFTER_HTML_BLOCK = 10


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

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
# Persistent session
# ---------------------------------------------------------------------------

_session: AsyncSession | None = None
_session_lock = asyncio.Lock()


async def init_session() -> None:
    """Open ONE AsyncSession that lives for the entire bot lifetime.
    Cookies accumulate in this session like a real browser."""
    global _session
    if _session is not None:
        return
    _session = AsyncSession(impersonate=_IMPERSONATE, timeout=_REQ_TIMEOUT)
    logger.info(
        "[parser] curl-cffi session opened (impersonate=%s, timeout=%ds)",
        _IMPERSONATE, _REQ_TIMEOUT,
    )


async def close_session() -> None:
    global _session
    if _session is None:
        return
    try:
        await _session.close()
    except Exception as e:
        logger.debug("[parser] session close err: %s", e)
    _session = None
    logger.info("[parser] curl-cffi session closed")


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------

async def rotate_ip() -> bool:
    """Hit the mobile-proxy rotation endpoint to get a fresh IP."""
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


def _proxies_dict() -> dict | None:
    if not config.proxy_list:
        return None
    p = config.proxy_list[0]
    return {"http": p, "https": p}


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _ensure_sort_by_date(url: str) -> str:
    """Append &s=104 (sort by date desc) without re-encoding the URL."""
    if re.search(r"[?&]s=\d+", url):
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + "s=104"


# ---------------------------------------------------------------------------
# Public API — scrape with smart per-error-type retry
# ---------------------------------------------------------------------------

# Status enum for _scrape_once
_OK = "ok"
_FAIL_403 = "fail_403"
_FAIL_429 = "fail_429"
_FAIL_HTML_BLOCK = "fail_html_block"
_FAIL_TIMEOUT = "fail_timeout"
_FAIL_OTHER = "fail_other"


async def fetch_search_items(url: str, *_unused) -> list[AvitoItem] | None:
    """Scrape the URL with smart retry. At most 2 attempts per call.

    Retry strategy by error type:
      403            -> rotate IP, wait 10s, retry
      429            -> wait 30s WITHOUT rotating (rotating makes it worse)
      HTML block     -> rotate IP, wait 10s, retry
      timeout        -> wait 15s, same IP, retry  (not a block, just network)
      anything else  -> no retry (genuine failure)

    Caller (scheduler) tracks consecutive failures and applies its own
    longer back-off after 3 cycles in a row."""
    if _session is None:
        logger.error("[parser] session not initialised — call init_session() first")
        return None

    target_url = _ensure_sort_by_date(url)

    last_status: str = _FAIL_OTHER
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        status, items = await _scrape_once(target_url)
        last_status = status

        if status == _OK:
            logger.info(
                "[parser] OK %d items for %s (attempt %d)",
                len(items or []), target_url[:80], attempt,
            )
            return items

        if attempt >= _MAX_ATTEMPTS:
            break

        # Per-error backoff before retry. _scrape_once always rotates
        # IP at the start, so we don't rotate here — just wait the right
        # amount based on the error type.
        if status == _FAIL_403:
            logger.warning(
                "[parser] 403 on attempt %d — waiting %ds before retry",
                attempt, _WAIT_AFTER_403,
            )
            await asyncio.sleep(_WAIT_AFTER_403)
        elif status == _FAIL_429:
            logger.warning(
                "[parser] 429 on attempt %d — waiting %ds before retry",
                attempt, _WAIT_AFTER_429,
            )
            await asyncio.sleep(_WAIT_AFTER_429)
        elif status == _FAIL_HTML_BLOCK:
            logger.warning(
                "[parser] HTML block on attempt %d — waiting %ds before retry",
                attempt, _WAIT_AFTER_HTML_BLOCK,
            )
            await asyncio.sleep(_WAIT_AFTER_HTML_BLOCK)
        elif status == _FAIL_TIMEOUT:
            logger.warning(
                "[parser] timeout on attempt %d — waiting %ds before retry",
                attempt, _WAIT_AFTER_TIMEOUT,
            )
            await asyncio.sleep(_WAIT_AFTER_TIMEOUT)
        else:
            # Hard non-retryable failure (HTTP 500/etc / network unreachable)
            logger.warning("[parser] hard fail on attempt %d (status=%s)", attempt, status)
            return None

    logger.error(
        "[parser] both attempts failed for %s (last=%s)",
        target_url[:80], last_status,
    )
    return None


async def _scrape_once(target_url: str) -> tuple[str, list[AvitoItem] | None]:
    """One scrape attempt via the persistent curl-cffi session.

    Always rotates the mobile-proxy IP BEFORE the request and waits 3s.
    Avito rate-limits per source IP — by the time we detect a 429 the
    current IP is already burned, so the only working strategy is to
    use a fresh IP for every single request (mobile proxies exist for
    exactly this).

    NO manual headers — curl-cffi's impersonate sets the full Chrome
    header set so that User-Agent and TLS fingerprint match. Any manual
    override would break that and trip Avito's bot detector."""
    # Always rotate before request — spec: "don't economize on IPs"
    await rotate_ip()
    await asyncio.sleep(3)

    proxies = _proxies_dict()

    try:
        async with _session_lock:
            resp = await _session.get(target_url, proxies=proxies)
    except Exception as e:
        msg = str(e).lower()
        if "timeout" in msg or "timed out" in msg or "operation_timedout" in msg:
            logger.warning("[parser] TIMEOUT: %s", str(e)[:120])
            return _FAIL_TIMEOUT, None
        logger.warning("[parser] HTTP error: %s", str(e)[:120])
        return _FAIL_OTHER, None

    sc = resp.status_code
    if sc == 403:
        logger.warning("[parser] HTTP 403")
        return _FAIL_403, None
    if sc == 429:
        logger.warning("[parser] HTTP 429")
        return _FAIL_429, None
    if sc in (301, 302, 303, 307, 308):
        logger.warning("[parser] HTTP %d redirect (treating as block)", sc)
        return _FAIL_HTML_BLOCK, None
    if sc != 200:
        logger.warning("[parser] HTTP %d", sc)
        return _FAIL_OTHER, None

    html = resp.text or ""
    if not html:
        logger.warning("[parser] empty body")
        return _FAIL_OTHER, None

    if _looks_like_block(html[:5000]):
        logger.warning("[parser] HTML block detected (body sniff)")
        return _FAIL_HTML_BLOCK, None

    items = _extract_items_from_html(html)
    if items is None:
        logger.warning("[parser] could not extract items from HTML (size=%d)", len(html))
        return _FAIL_OTHER, None
    return _OK, items


_BLOCK_PHRASES = (
    "доступ ограничен",
    "проблема с ip",
    "подозрительная активность",
    "слишком много запросов",
    "robot check",
)


def _looks_like_block(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(p in t for p in _BLOCK_PHRASES)


# ---------------------------------------------------------------------------
# HTML extraction — pull items out of <script data-mfe-state> JSON blobs
# ---------------------------------------------------------------------------

_MFE_SCRIPT_RE = re.compile(
    r'<script[^>]*data-mfe-state="true"[^>]*>([\s\S]+?)</script>',
    re.IGNORECASE,
)

# The ONE and ONLY path that holds the main search results. Avito's MFE
# architecture puts the search catalog under state.data.catalog. Any other
# `items` array on the page belongs to a different widget (recently viewed,
# recommendations, similar items, banners, etc.) and MUST NOT be read.
_MAIN_CATALOG_PATH = ("state", "data", "catalog", "items")
# Older variants seen on some pages
_FALLBACK_CATALOG_PATHS = (
    ("data", "catalog", "items"),
    ("initialData", "catalog", "items"),
    ("pageProps", "catalog", "items"),
)


def _walk_path(data, path: tuple[str, ...]):
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node if isinstance(node, list) else None


def _extract_items_from_html(html: str) -> list[AvitoItem] | None:
    """Parse the MAIN search catalog out of the HTML.

    Strategy: iterate every <script data-mfe-state> blob, parse its JSON,
    and accept ONLY items found at the exact main-catalog path. Anything
    that doesn't have that exact path is ignored — even if it has its own
    `items` array, it is not the search result.

    Falls back to legacy hydration vars (__initialData__ / __preloadedState__
    / __mfe__) using the same exact-path lookup."""
    blobs = _MFE_SCRIPT_RE.findall(html)
    logger.info("[parser] %d mfe-state scripts on page", len(blobs))

    # 1) mfe-state scripts — main path first
    for idx, blob in enumerate(blobs):
        blob = blob.strip()
        if not blob or "sandbox" in blob[:200]:
            continue
        try:
            data = orjson.loads(html_lib.unescape(blob))
        except Exception:
            try:
                data = orjson.loads(blob)
            except Exception:
                continue
        if not isinstance(data, dict):
            continue

        items_raw = _walk_path(data, _MAIN_CATALOG_PATH)
        if items_raw is not None:
            logger.info(
                "[parser] mfe script #%d: MAIN CATALOG found at %s (%d raw items)",
                idx, ".".join(_MAIN_CATALOG_PATH), len(items_raw),
            )
            items = _items_from_raw_list(items_raw)
            if items:
                logger.info(
                    "[parser] extraction source: mfe-state #%d / %s — %d listings",
                    idx, ".".join(_MAIN_CATALOG_PATH), len(items),
                )
                return items

        # Log what this script DOES have so we can spot if Avito changed paths
        top_keys = list(data.keys())[:6]
        logger.debug("[parser] mfe script #%d: no main catalog (top keys: %s)", idx, top_keys)

    # 2) Same scripts again, but with fallback paths — covers older page variants
    for idx, blob in enumerate(blobs):
        blob = blob.strip()
        if not blob or "sandbox" in blob[:200]:
            continue
        try:
            data = orjson.loads(html_lib.unescape(blob))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for path in _FALLBACK_CATALOG_PATHS:
            items_raw = _walk_path(data, path)
            if items_raw is not None:
                items = _items_from_raw_list(items_raw)
                if items:
                    logger.info(
                        "[parser] extraction source: mfe-state #%d / %s (fallback) — %d listings",
                        idx, ".".join(path), len(items),
                    )
                    return items

    # 3) window.__initialData__ — legacy hydration var
    m = re.search(r'window\.__initialData__\s*=\s*"(.+?)"\s*;', html, re.DOTALL)
    if m:
        try:
            data = orjson.loads(unquote(m.group(1)))
            for path in (_MAIN_CATALOG_PATH,) + _FALLBACK_CATALOG_PATHS:
                items_raw = _walk_path(data, path)
                if items_raw is not None:
                    items = _items_from_raw_list(items_raw)
                    if items:
                        logger.info(
                            "[parser] extraction source: __initialData__ / %s — %d listings",
                            ".".join(path), len(items),
                        )
                        return items
        except Exception as e:
            logger.debug("[parser] __initialData__ decode err: %s", e)

    # 4) window.__preloadedState__ / __mfe__
    for var_name in ("__preloadedState__", "__mfe__"):
        marker = f"window.{var_name}"
        idx = html.find(marker)
        if idx < 0:
            continue
        eq_idx = html.find("=", idx)
        if eq_idx < 0:
            continue
        start = eq_idx + 1
        while start < len(html) and html[start] in " \t\n\r":
            start += 1
        if start >= len(html):
            continue
        try:
            data = None
            if html[start] == '"':
                end = html.find('";', start + 1)
                if end < 0:
                    end = html.find('"', start + 1)
                if end > start:
                    data = orjson.loads(unquote(html[start + 1:end]))
            elif html[start] == "{":
                depth = 0
                i = start
                while i < min(len(html), start + 5_000_000):
                    if html[i] == "{":
                        depth += 1
                    elif html[i] == "}":
                        depth -= 1
                        if depth == 0:
                            data = orjson.loads(html[start:i + 1])
                            break
                    i += 1
            if data is None:
                continue
            for path in (_MAIN_CATALOG_PATH,) + _FALLBACK_CATALOG_PATHS:
                items_raw = _walk_path(data, path)
                if items_raw is not None:
                    items = _items_from_raw_list(items_raw)
                    if items:
                        logger.info(
                            "[parser] extraction source: %s / %s — %d listings",
                            var_name, ".".join(path), len(items),
                        )
                        return items
        except Exception as e:
            logger.debug("[parser] %s parse err: %s", var_name, e)

    return None


def _items_from_raw_list(items_raw) -> list[AvitoItem]:
    """Convert the raw catalog.items list into AvitoItems. Skips wrappers
    that are not 'item' type (banners/snippets if Avito ever inlines them
    into the catalog)."""
    items: list[AvitoItem] = []
    for raw_item in items_raw:
        if not isinstance(raw_item, dict):
            continue
        rtype = (raw_item.get("type") or "").strip()
        if rtype and rtype != "item":
            continue
        val = raw_item.get("value", raw_item) if "value" in raw_item else raw_item
        if not isinstance(val, dict):
            continue
        if not (val.get("id") or val.get("itemId")):
            continue
        try:
            items.append(_parse_item(val))
        except Exception as e:
            logger.debug("[parser] parse item err: %s", e)
    return items


# ---------------------------------------------------------------------------
# Catalog item parsing
# ---------------------------------------------------------------------------


def _parse_item(val: dict) -> AvitoItem:
    avito_id = str(val.get("id") or val.get("itemId") or "")
    title = val.get("title") or ""

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

    url_path = val.get("urlPath") or val.get("url") or ""
    if url_path and not url_path.startswith("http"):
        item_url = f"https://www.avito.ru{url_path}"
    else:
        item_url = url_path or "https://www.avito.ru"

    image_url = _extract_image_url(val)

    location = None
    loc = val.get("location") or {}
    if isinstance(loc, dict):
        location = loc.get("name")

    desc = val.get("description") or ""
    if isinstance(desc, dict):
        desc = desc.get("text") or ""
    if desc and len(desc) > 300:
        desc = desc[:300] + "..."

    seller = val.get("seller") or {}
    seller_name = seller.get("name") if isinstance(seller, dict) else None

    ts_raw = (
        val.get("sortTimeStamp")
        or val.get("time")
        or val.get("publishDate")
        or val.get("sortTime")
    )
    ts: int | None = None
    if isinstance(ts_raw, (int, float)):
        ts_int = int(ts_raw)
        if ts_int > 1_000_000_000_000:
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
# Image extraction
# ---------------------------------------------------------------------------

_IMAGE_SIZE_KEYS = (
    # Modern Avito catalog uses square crops
    "864x864", "636x636", "472x472", "432x432",
    # Older/rectangular crops still appear in some payloads
    "864x648", "636x476", "540x405", "432x324", "318x238",
    "main", "big", "biggest", "default", "url",
)


def _extract_image_url(val: dict) -> str | None:
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
        elif isinstance(obj, str) and obj.startswith("http"):
            return obj
    return None


def _image_from_dict(obj) -> str | None:
    if isinstance(obj, str) and obj.startswith("http"):
        return obj
    if not isinstance(obj, dict):
        return None
    for k in _IMAGE_SIZE_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    for nest_key in ("variants", "sizes", "urls"):
        nested = obj.get(nest_key)
        if isinstance(nested, dict):
            for k in _IMAGE_SIZE_KEYS:
                v = nested.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    return v
            for v in nested.values():
                if isinstance(v, str) and v.startswith("http"):
                    return v
    for v in obj.values():
        if isinstance(v, str) and v.startswith("http") and (
            ".jpg" in v or ".jpeg" in v or ".png" in v or ".webp" in v
        ):
            return v
    return None
