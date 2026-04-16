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
import random
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

_REQ_TIMEOUT = 45  # seconds — generous for Avito's slow pages

# Pool of impersonation targets — picked at random for every request.
# Fixing on a single TLS fingerprint makes Avito's behavioral detector
# trivially flag us. Each value here makes curl-cffi mimic that browser's
# TLS handshake AND default header set, so they go together — never
# override headers manually.
_IMPERSONATE_OPTIONS = ("chrome119", "chrome120", "chrome124", "safari17_0")

# Diagnostic — log the actual headers curl-cffi sends ONCE, on the first
# successful request, so we can verify nothing custom is leaking through.
_headers_logged = False

# IP rotation is throttled globally — at most once every 5 minutes,
# regardless of how many subscriptions hit a block. Rotating on every
# error makes the proxy pool look bot-like (many IPs, same behavior).
_MIN_ROTATION_INTERVAL = 300  # seconds
_last_rotation_ts: float = 0.0
_rotation_lock = asyncio.Lock()

# GLOBAL lock for ALL Avito requests across the whole bot (scheduler AND
# handlers' initial scan). Held for IP-check + scrape + post-cooldown so
# two requests can never overlap and there's always a randomized 10-20s
# gap between consecutive scrapes from this process.
_avito_request_lock = asyncio.Lock()
_POST_REQUEST_MIN = 10
_POST_REQUEST_MAX = 20


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
# Session lifecycle (no-op stubs)
# ---------------------------------------------------------------------------
# We deliberately do NOT keep a persistent AsyncSession anymore — a fresh
# session is created per request inside _do_scrape. Banned cookies from a
# previous block would otherwise carry over and re-trigger the ban.

async def init_session() -> None:
    proxies = _proxies_dict()
    logger.info(
        "[parser] per-request session mode (timeout=%ds, proxy=%s, verify=False)",
        _REQ_TIMEOUT, "yes" if proxies else "no",
    )


async def close_session() -> None:
    pass


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


async def throttled_rotate_ip() -> bool:
    """Rotate IP at most once every _MIN_ROTATION_INTERVAL seconds (5 min).
    If another scrape just rotated, skip — multiple subs hitting a block in
    a short window must NOT cause multiple rotations."""
    global _last_rotation_ts
    import time
    async with _rotation_lock:
        now = time.monotonic()
        elapsed = now - _last_rotation_ts
        if elapsed < _MIN_ROTATION_INTERVAL:
            logger.info(
                "[parser] skipping IP rotation — last one was %.0fs ago "
                "(min interval %ds)",
                elapsed, _MIN_ROTATION_INTERVAL,
            )
            return False
        ok = await rotate_ip()
        if ok:
            _last_rotation_ts = now
            logger.info("[parser] IP rotated (next allowed in %ds)", _MIN_ROTATION_INTERVAL)
        return ok


async def check_proxy_ip() -> str | None:
    """Used at startup only — separate httpx client to verify proxy creds."""
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


async def _log_sent_headers(impersonate: str) -> None:
    """One-off probe to httpbin/headers. Logs what server actually receives,
    so we can confirm only impersonate-set headers are sent and no manual
    overrides leak. Uses its OWN short-lived session via the proxy."""
    proxies = _proxies_dict()
    try:
        async with AsyncSession(
            impersonate=impersonate, timeout=15, proxies=proxies, verify=False,
        ) as s:
            r = await s.get("https://httpbin.org/headers")
            if r.status_code == 200:
                data = r.json() or {}
                headers = data.get("headers", {})
                logger.info("[parser] SENT HEADERS (impersonate=%s, via httpbin):", impersonate)
                for k, v in headers.items():
                    logger.info("[parser]   %s: %s", k, v)
            else:
                logger.warning("[parser] httpbin probe HTTP %d", r.status_code)
    except Exception as e:
        logger.warning("[parser] header probe failed: %s", str(e)[:120])


async def _log_visible_ip(session: AsyncSession, label: str = "") -> None:
    """Hit api.ipify.org through the given session and log the IP.

    Uses the SAME session that will then talk to Avito so we see the
    exact IP/proxy path the scrape will use."""
    try:
        r = await session.get("https://api.ipify.org?format=json")
        if r.status_code == 200:
            ip = (r.json() or {}).get("ip", "?")
            logger.info("[parser] visible IP %s%s", ip, f" ({label})" if label else "")
        else:
            logger.warning("[parser] ipify HTTP %d", r.status_code)
    except Exception as e:
        logger.warning("[parser] visible-IP check failed: %s", str(e)[:120])


def _proxies_dict() -> dict | None:
    """Convert proxy URL into curl-cffi's expected dict shape.

    Required form: {"http": "scheme://user:pass@host:port",
                    "https": "scheme://user:pass@host:port"}
    Both keys must be set so curl-cffi tunnels HTTP and HTTPS the same way."""
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
# Public API — ONE request per call. NO retries.
# ---------------------------------------------------------------------------
#
# Strategy (per spec):
#   - One monitoring cycle = one request. Period.
#   - Any error -> log, return None. Caller (scheduler) waits 90-180s for
#     the next cycle and after 3 failed cycles in a row pauses 10 minutes.
#   - IP rotation only on 403 / HTML block, throttled to once per 5 minutes
#     globally. On 429 we DO NOT rotate (rotating on 429 makes it worse —
#     Avito sees many fresh IPs all behaving the same way and rate-limits
#     the whole pool).
#   - No pre-request rotation. Same IP across requests until something
#     actually breaks.

async def fetch_search_items(url: str, *_unused) -> list[AvitoItem] | None:
    """Single request, no retry. Returns parsed items on success, None on
    any failure. The scheduler is responsible for back-off.

    Acquires the GLOBAL avito-request lock for the whole call: IP check +
    scrape + 10-20s post-cooldown all happen under the lock. This makes
    it impossible for two scrapes to run in parallel — even when the
    handler's initial scan races with a scheduler cycle."""
    target_url = _ensure_sort_by_date(url)

    async with _avito_request_lock:
        try:
            return await _do_scrape(target_url)
        finally:
            # Mandatory cooldown BEFORE releasing the lock so the next
            # caller starts at least 10-20s after the previous request
            # finished — never instant succession.
            cooldown = random.randint(_POST_REQUEST_MIN, _POST_REQUEST_MAX)
            logger.debug("[parser] post-request cooldown %ds (lock held)", cooldown)
            await asyncio.sleep(cooldown)


async def _do_scrape(target_url: str) -> list[AvitoItem] | None:
    """The actual scrape. Caller MUST hold _avito_request_lock.

    Creates a FRESH AsyncSession for every request — banned cookies from
    a previous block must not carry over and re-trigger the ban.

    Random impersonate target per request — fixing on one TLS fingerprint
    makes Avito's behavioural detector flag us trivially.

    NO custom headers — curl-cffi's impersonate already sets the full
    browser-correct header set. Adding User-Agent / Accept / Cookie etc.
    by hand desyncs them from the TLS fingerprint and is a bot signal."""
    impersonate = random.choice(_IMPERSONATE_OPTIONS)
    proxies = _proxies_dict()

    async with AsyncSession(
        impersonate=impersonate,
        timeout=_REQ_TIMEOUT,
        proxies=proxies,
        verify=False,
    ) as session:
        # Diagnostic: log the IP Avito will see for THIS request via the
        # same session (so we know it's the same proxy path).
        await _log_visible_ip(session, f"pre-scrape, impersonate={impersonate}")

        try:
            resp = await session.get(target_url)
        except Exception as e:
            msg = str(e).lower()
            if "timeout" in msg or "timed out" in msg or "operation_timedout" in msg:
                logger.warning("[parser] TIMEOUT for %s: %s", target_url[:80], str(e)[:120])
            else:
                logger.warning("[parser] HTTP error for %s: %s", target_url[:80], str(e)[:120])
            return None

        # First-time diagnostic: ask httpbin what it received from us so we
        # can verify curl-cffi sends ONLY the impersonate-generated headers
        # and nothing custom is leaking through. (curl-cffi's resp.request
        # .headers is empty — headers are set by libcurl on the C side.)
        global _headers_logged
        if not _headers_logged:
            _headers_logged = True  # set first so a probe failure doesn't repeat
            await _log_sent_headers(impersonate)

        sc = resp.status_code

        if sc == 200:
            html = resp.text or ""
            if not html:
                logger.warning("[parser] empty body for %s", target_url[:80])
                return None
            if _looks_like_block(html[:5000]):
                logger.warning(
                    "[parser] HTML block detected for %s (impersonate=%s) — rotating IP (throttled)",
                    target_url[:80], impersonate,
                )
                await throttled_rotate_ip()
                return None
            items = _extract_items_from_html(html)
            if items is None:
                logger.warning(
                    "[parser] could not extract items from HTML for %s (size=%d)",
                    target_url[:80], len(html),
                )
                return None
            logger.info(
                "[parser] OK %d items for %s (impersonate=%s)",
                len(items), target_url[:80], impersonate,
            )
            return items

        if sc == 403:
            logger.warning(
                "[parser] HTTP 403 for %s (impersonate=%s) — rotating IP (throttled)",
                target_url[:80], impersonate,
            )
            await throttled_rotate_ip()
            return None

        if sc == 429:
            # Spec: do NOT rotate on 429. Just log and let scheduler retry next
            # cycle. Rotating on 429 makes Avito ban the whole proxy pool.
            logger.warning(
                "[parser] HTTP 429 for %s (impersonate=%s) — NOT rotating",
                target_url[:80], impersonate,
            )
            return None

        if sc in (301, 302, 303, 307, 308):
            logger.warning(
                "[parser] HTTP %d redirect for %s (impersonate=%s) — block, rotating IP",
                sc, target_url[:80], impersonate,
            )
            await throttled_rotate_ip()
            return None

        logger.warning("[parser] HTTP %d for %s (impersonate=%s)", sc, target_url[:80], impersonate)
        return None


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
