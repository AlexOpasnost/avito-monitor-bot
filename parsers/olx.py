"""OLX (olx.pl / olx.ua / olx.ro / olx.com.br / …) marketplace parser.

OLX has a public-but-unauthenticated JSON API at /api/v1/offers that
returns full item data (photos, prices with currency, absolute ISO 8601
dates, description, seller) — much richer than scraping the DOM.

The listing HTML server-side-renders only the first ~5 cards with real
<img src> URLs; everything below the fold is a placeholder SVG (the
real URL never arrives until JS scrolls into view). So for reliable
photos on every item we MUST use the API.

Strategy:
  1. If the URL path matches /oferty/q-<term>/ we have a free-text
     search and we can build the API URL directly (query=<term>).
  2. Otherwise the URL is category-based; we fetch the HTML once,
     extract `CID<N>-` from any item href, and use category_id=<N>.
  3. Any `search[...]=...` params from the original URL are forwarded
     to the API as-is.
Promoted/top_ad listings are filtered out so the monitor stays in sync
with the user's "newest first" sort.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
import time
from datetime import datetime
from urllib.parse import quote, urlparse

import orjson

from .base import SearchItem
from .common import (
    MAX_JSON_BYTES,
    download_image_bytes,
    get_cloudscraper,
    global_request_lock,
    host_matches_pattern,
    invalidate_session,
    proxies_dict,
    rotate_ip,
)

logger = logging.getLogger(__name__)

_HOST = "olx"
# Anchored to the *hostname* (not the URL), via fullmatch in
# host_matches_pattern. The TLD shape stays permissive (OLX runs in 30+
# countries) but a query-string substring like `?u=https://olx.pl/...`
# can no longer slip through — host_matches_pattern parses the URL and
# matches against the hostname only.
_OLX_HOST_RE = re.compile(
    r"(?:www\.|m\.)?olx\.(?:com\.[a-z]{2}|[a-z]{2,3})",
    re.IGNORECASE,
)

# Photo CDN template: the API returns links like
# "https://ireland.apollo.olxcdn.com:443/v1/files/<id>-PL/image;s={width}x{height}"
# We fill in a fixed size — 600x600 is big enough for a Telegram photo
# and the CDN supports arbitrary sizes.
_PHOTO_W, _PHOTO_H = 600, 600

# Rough USD exchange rates (April 2026). Used only for a parenthetical
# "~N $" hint next to the native price; precision doesn't matter much
# for a monitor. Can be overridden via env later if needed.
_USD_RATES: dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.08,        # olx.pt and cross-region
    "PLN": 0.25,        # olx.pl
    "UAH": 0.024,       # olx.ua
    "RON": 0.22,        # olx.ro
    "BGN": 0.57,        # olx.bg
    "BRL": 0.20,        # olx.com.br
    "KZT": 0.002,       # olx.kz
    "UZS": 0.000077,    # olx.uz
    "BAM": 0.58,        # olx.ba
    "RUB": 0.011,       # rare but possible
}

# Marker for paid top-slot cards — skip these when sampling for category.
_PROMOTED_MARKERS = (
    "search_reason=search%7Cpromoted",
    "search_reason=search|promoted",
)

# Category_id lookup is expensive (one HTML fetch + one API call). Cache
# by user-URL with a 1-hour TTL; category mappings practically never
# change.
_CATEGORY_CACHE: dict[str, tuple[int, float]] = {}
_CATEGORY_TTL = 3600.0
# Bound the cache so a churn of unique URLs doesn't grow it forever.
_CATEGORY_CACHE_MAX = 2000


class OlxSource:
    name = "olx"

    def matches(self, url: str) -> bool:
        return host_matches_pattern(url, _OLX_HOST_RE)

    async def fetch(
        self, url: str, proxy: str | None, max_retries: int = 3,
    ) -> list[SearchItem] | None:
        async with global_request_lock():
            try:
                return await _fetch_inner(url, proxy, max_retries)
            finally:
                import random
                cooldown = random.uniform(3.0, 7.0)
                logger.info("[olx] post-request cooldown %.1fs (lock held)", cooldown)
                await asyncio.sleep(cooldown)


# ---------------------------------------------------------------------------
# Fetch pipeline
# ---------------------------------------------------------------------------

async def _fetch_inner(url: str, proxy: str | None, max_retries: int) -> list[SearchItem] | None:
    api_url = await _build_api_url(url, proxy)
    if api_url is None:
        logger.error("[olx] could not build API URL for %s", url[:100])
        return None
    for attempt in range(max_retries):
        items, blocked = await _fetch_api(api_url, url, proxy)
        if items is not None:
            logger.info("[olx] fetched %d items for %s", len(items), url[:80])
            return items
        if not blocked:
            return None
        logger.warning(
            "[olx] blocked (attempt %d/%d), rotating session%s",
            attempt + 1, max_retries, " + IP" if proxy else "",
        )
        invalidate_session(_HOST)
        if proxy:
            await rotate_ip()
        await asyncio.sleep(5)
    logger.error("[olx] all %d attempts blocked for %s", max_retries, url[:80])
    return None


async def _fetch_api(api_url: str, user_url: str, proxy: str | None):
    loop = asyncio.get_running_loop()
    resp_data = await loop.run_in_executor(
        None, lambda: _fetch_api_sync(api_url, proxy),
    )
    if resp_data is None:
        return None, False
    status, body = resp_data
    if status in (429, 403):
        logger.warning("[olx] API BLOCKED %d", status)
        return None, True
    if status != 200:
        logger.debug("[olx] API HTTP %d for %s", status, api_url[:100])
        return None, False
    if len(body) > MAX_JSON_BYTES:
        logger.warning("[olx] API body oversized: %d bytes", len(body))
        return None, False
    try:
        data = orjson.loads(body)
    except Exception as e:
        logger.warning("[olx] API JSON decode err: %s", str(e)[:80])
        return None, False
    items = _parse_api_response(data, user_url)
    return items, False


def _fetch_api_sync(api_url: str, proxy: str | None):
    try:
        origin = _origin_for(api_url) or "https://www.olx.pl"
        s = get_cloudscraper(_HOST, warmup_urls=[origin + "/"], proxy=proxy)
        proxies = proxies_dict(proxy)
        headers = {"Accept": "application/json"}
        logger.info("[olx] API REQUEST url=%r", api_url)
        # allow_redirects=False blocks redirect-based SSRF: the URL
        # already passed the hostname allowlist, but a 302 from a
        # compromised CDN/edge could send us to 127.0.0.1 / cloud
        # metadata. /api/v1/offers responds 200 directly when the
        # request is well-formed; if we ever hit a 30x here we want
        # to log it as a warning, not silently follow.
        resp = s.get(
            api_url, proxies=proxies, timeout=30, headers=headers,
            allow_redirects=False,
        )
        logger.info("[olx] API response status=%d, size=%d",
                    resp.status_code, len(resp.text))
        return resp.status_code, resp.text
    except Exception as e:
        logger.debug("[olx] API sync fetch error: %s", e)
        return None


# ---------------------------------------------------------------------------
# URL → API URL translation
# ---------------------------------------------------------------------------

# Matches a query-term segment: /oferty/q-<slug>/ or /q-<slug>/
_QUERY_IN_PATH_RE = re.compile(r"/q-([^/?]+)", re.IGNORECASE)


async def _build_api_url(url: str, proxy: str | None) -> str | None:
    p = urlparse(url)
    if not p.netloc:
        return None
    origin = f"{p.scheme or 'https'}://{p.netloc}"
    base = f"{origin}/api/v1/offers"

    # Parse the user's query string into (key, value) pairs, preserving
    # encoded brackets so search[filter_enum_state][0]=new passes through
    # the API unchanged. Drop any ordering from the user URL — we always
    # force newest-first below so the monitor catches fresh items, not
    # whatever OLX's default "relevance" ranking dredges up.
    original_pairs = [
        (k, v) for k, v in _parse_raw_query(p.query or "")
        if k.lower() not in ("search[order]", "sort_by")
    ]
    # Always newest-first. Without this the API returns items in an
    # opaque mix (relevance + recency + boost) and the bot picks up
    # refreshed-4-hours-ago ads as if they were brand new.
    sort_pair = ("sort_by", "created_at:desc")

    # Strategy 1: free-text query like /oferty/q-iphone/
    m = _QUERY_IN_PATH_RE.search(p.path or "")
    if m:
        term = m.group(1).replace("-", " ")
        pairs = [("query", term), sort_pair] + original_pairs
        return base + "?" + _encode_pairs(pairs)

    # Strategy 2: category-based path — fetch the HTML listing once,
    # extract the category id from any item href's "CID<N>-" marker.
    cid = await _extract_category_id(url, proxy)
    if cid is not None:
        pairs = [("category_id", str(cid)), sort_pair] + original_pairs
        return base + "?" + _encode_pairs(pairs)

    # No safe strategy — abort. Returning the API base with just the
    # filters would give a cross-category garbage listing (observed:
    # laptop-OS filter + state=new returned audiobooks, clothing, shoes,
    # furniture). Better to fail loud and let the scheduler retry.
    logger.error(
        "[olx] could not resolve category_id for %s; giving up this cycle",
        url[:100],
    )
    return None


def _parse_raw_query(q: str) -> list[tuple[str, str]]:
    """Split a raw query string, tolerant of bracket notation.

    Values are returned URL-decoded; we re-encode when reassembling.
    """
    from urllib.parse import unquote
    out: list[tuple[str, str]] = []
    if not q:
        return out
    for chunk in q.split("&"):
        if not chunk:
            continue
        if "=" in chunk:
            k, v = chunk.split("=", 1)
        else:
            k, v = chunk, ""
        out.append((unquote(k), unquote(v)))
    return out


def _encode_pairs(pairs) -> str:
    # quote() with safe="[]" preserves bracket notation the API expects
    # for search[filter_enum_...][N]=... keys.
    return "&".join(
        f"{quote(k, safe='[]')}={quote(v, safe='[],:%')}" for k, v in pairs
    )


async def _extract_category_id(url: str, proxy: str | None) -> int | None:
    """Return the numeric OLX category_id for this listing URL.

    The `CID<N>-` in item hrefs is NOT reliable — on almost every OLX
    listing that value is the parent domain (e.g. 99 = electronics),
    not the specific sub-category the URL is actually filtered by.
    We work around it by picking the first organic card id off the
    HTML, calling /api/v1/offers/<id> and reading `data.category.id`.
    """
    cached = _CATEGORY_CACHE.get(url)
    now = time.time()
    if cached and (now - cached[1]) < _CATEGORY_TTL:
        return cached[0]

    loop = asyncio.get_running_loop()
    html_text = await loop.run_in_executor(
        None, lambda: _fetch_html_head_sync(url, proxy),
    )
    if not html_text:
        return None

    item_id = _first_organic_card_id(html_text)
    if item_id is None:
        logger.warning("[olx] no organic l-card found in HTML of %s", url[:100])
        return None

    cid = await _lookup_category_via_item(url, item_id, proxy)
    if cid is None:
        logger.warning(
            "[olx] item %d lookup returned no category.id for %s",
            item_id, url[:100],
        )
        return None
    if len(_CATEGORY_CACHE) >= _CATEGORY_CACHE_MAX:
        keys = list(_CATEGORY_CACHE.keys())[: _CATEGORY_CACHE_MAX // 2]
        for k in keys:
            _CATEGORY_CACHE.pop(k, None)
    _CATEGORY_CACHE[url] = (cid, now)
    logger.info("[olx] resolved category_id=%d via item %d (cached 1h)", cid, item_id)
    return cid


def _first_organic_card_id(html: str) -> int | None:
    """Return the id attr of the first `<div data-cy="l-card">` on the
    page whose inner anchor does NOT carry a paid-promotion marker.

    We use BeautifulSoup instead of a regex so we don't get tripped up
    by (a) attribute order / inline <style> blocks between data-cy and
    id, or (b) the card boundaries that make a windowed promoted-check
    unreliable (OLX cards emit ~60 KB of inlined CSS each).
    """
    if not html:
        return None
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for card in soup.select('[data-cy="l-card"]'):
        card_id = (card.get("id") or "").strip()
        if not card_id.isdigit():
            continue
        # Look at every <a> anchor inside the card. A card is promoted
        # if ANY of its anchors carries `search_reason=search|promoted`.
        anchors = card.select('a[href]')
        if any(_is_promoted_href(a.get("href") or "") for a in anchors):
            continue
        try:
            return int(card_id)
        except ValueError:
            continue
    return None


def _is_promoted_href(href: str) -> bool:
    return any(marker in href for marker in _PROMOTED_MARKERS)


async def _lookup_category_via_item(
    base_url: str, item_id: int, proxy: str | None,
) -> int | None:
    origin = _origin_for(base_url) or "https://www.olx.pl"
    api_url = f"{origin}/api/v1/offers/{item_id}"
    loop = asyncio.get_running_loop()
    body = await loop.run_in_executor(
        None, lambda: _fetch_simple_sync(api_url, proxy),
    )
    if not body:
        return None
    if len(body) > MAX_JSON_BYTES:
        logger.warning("[olx] item %d body oversized: %d bytes", item_id, len(body))
        return None
    try:
        data = orjson.loads(body)
    except Exception as e:
        logger.debug("[olx] item %d JSON decode err: %s", item_id, str(e)[:80])
        return None
    cat = (data.get("data") or {}).get("category")
    if not isinstance(cat, dict):
        return None
    cid = cat.get("id")
    if isinstance(cid, int) and cid > 0:
        return cid
    if isinstance(cid, str) and cid.isdigit():
        return int(cid)
    return None


def _fetch_simple_sync(url: str, proxy: str | None) -> str | None:
    try:
        origin = _origin_for(url) or "https://www.olx.pl"
        s = get_cloudscraper(_HOST, warmup_urls=[origin + "/"], proxy=proxy)
        proxies = proxies_dict(proxy)
        logger.info("[olx] category probe: %s", url[:100])
        resp = s.get(url, proxies=proxies, timeout=20,
                     headers={"Accept": "application/json"},
                     allow_redirects=False)
        if resp.status_code != 200:
            logger.debug("[olx] category probe HTTP %d", resp.status_code)
            return None
        return resp.text
    except Exception as e:
        logger.debug("[olx] category probe error: %s", e)
        return None


def _fetch_html_head_sync(url: str, proxy: str | None) -> str | None:
    """Fetch the full HTML listing (no streaming truncation).

    Earlier versions read only the first 200 KB — but OLX inlines ~60 KB
    of CSS per l-card, so 200 KB often contains just 3 cards, ALL of
    which are paid promotions. That made category_id extraction fail,
    which in turn dropped the listing into a dangerous "bare filters"
    fallback that returned random cross-category items.
    """
    try:
        origin = _origin_for(url) or "https://www.olx.pl"
        s = get_cloudscraper(_HOST, warmup_urls=[origin + "/"], proxy=proxy)
        proxies = proxies_dict(proxy)
        logger.info("[olx] HTML probe for category_id: %s", url[:100])
        # See comment on the API path: redirect chains here would be
        # post-validation SSRF.
        resp = s.get(url, proxies=proxies, timeout=45, allow_redirects=False)
        if resp.status_code != 200:
            logger.warning("[olx] html probe HTTP %d", resp.status_code)
            return None
        return resp.text
    except Exception as e:
        logger.debug("[olx] html-head fetch error: %s", e)
        return None


def _origin_for(url: str) -> str:
    try:
        p = urlparse(url)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# API response parsing
# ---------------------------------------------------------------------------

def _parse_api_response(data: dict, user_url: str) -> list[SearchItem] | None:
    raw = data.get("data")
    if not isinstance(raw, list):
        logger.warning("[olx] API response has no data[] array, keys=%s",
                       list(data.keys())[:10])
        return None
    metadata = data.get("metadata") or {}
    total = metadata.get("total_elements") or metadata.get("visible_total_count")
    source = metadata.get("source") or {}
    organic_idx = source.get("organic")
    if not isinstance(organic_idx, list):
        organic_idx = None

    logger.info(
        "[olx] API listing: %d entries, total=%s, organic_idx=%s",
        len(raw), total,
        ("whole list" if organic_idx is None else f"{len(organic_idx)} indexes"),
    )

    items: list[SearchItem] = []
    skipped_promoted = 0
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        # Filter promoted / top_ad ads to keep the "newest first" order
        # stable across cycles.
        if organic_idx is not None and idx not in organic_idx:
            skipped_promoted += 1
            continue
        promo = entry.get("promotion") or {}
        if promo.get("top_ad"):
            skipped_promoted += 1
            continue
        try:
            items.append(_parse_api_item(entry))
        except Exception as e:
            logger.debug("[olx] parse api item err: %s", e)

    if items:
        total_cnt = len(items)
        with_image = sum(1 for i in items if i.image_url)
        with_loc = sum(1 for i in items if i.location)
        with_desc = sum(1 for i in items if i.description)
        with_ts = sum(1 for i in items if i.published_timestamp)
        logger.info(
            "[olx] completeness: image=%d/%d, location=%d/%d, desc=%d/%d, date=%d/%d"
            " (skipped %d promoted)",
            with_image, total_cnt, with_loc, total_cnt,
            with_desc, total_cnt, with_ts, total_cnt, skipped_promoted,
        )
    return items


def _parse_api_item(entry: dict) -> SearchItem:
    ext_id = str(entry.get("id") or "")
    title = (entry.get("title") or "").strip()
    item_url = (entry.get("url") or "").strip()
    if not item_url:
        # Fall back to a canonical-ish URL if api omitted it
        item_url = f"https://www.olx.pl/d/oferta/-ID{ext_id}.html"

    # Strip HTML from description, decode entities, trim whitespace.
    desc_raw = entry.get("description") or ""
    description = _clean_description(desc_raw) or None

    # Published timestamp: prefer last_refresh (user sees refreshed ads
    # as new), fall back to created.
    ts_str = entry.get("last_refresh_time") or entry.get("created_time")
    ts = _parse_iso(ts_str)

    # Price — extract from params list. Native render only; the
    # user-currency estimate is appended at notification-render time
    # by parsers.currency.format_with_estimate.
    price_str, price_value, currency = _parse_price(entry.get("params") or [])

    # Location
    loc = _parse_location(entry.get("location") or {})

    # First photo
    image_url = _extract_photo(entry.get("photos") or [])

    # Seller name
    user = entry.get("user") or {}
    seller = (user.get("name") or "").strip() or None

    return SearchItem(
        source="olx",
        external_id=ext_id,
        title=title,
        price=price_str,
        price_value=price_value,
        url=item_url,
        image_url=image_url,
        location=loc,
        description=description,
        seller_name=seller,
        published_timestamp=ts,
        currency=(currency.upper() if currency else None),
    )


def _parse_iso(s) -> int | None:
    """Parse "2026-04-20T18:55:59+02:00" → unix seconds (int).

    datetime.fromisoformat in Python 3.11 handles offset natively; the
    result is tz-aware, so .timestamp() gives correct UTC seconds.
    """
    if not isinstance(s, str) or not s:
        return None
    try:
        # Python 3.11 accepts "+HH:MM" and trailing "Z"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp())
    except ValueError:
        return None


def _parse_price(params) -> tuple[str, int | None, str | None]:
    if not isinstance(params, list):
        return "Цена не указана", None, None
    price_obj = None
    for p in params:
        if isinstance(p, dict) and p.get("key") == "price":
            v = p.get("value")
            if isinstance(v, dict):
                price_obj = v
                break
    if price_obj is None:
        return "Цена не указана", None, None
    label = (price_obj.get("label") or "").strip() or None
    value_raw = price_obj.get("value")
    currency = price_obj.get("currency")
    price_value = None
    if isinstance(value_raw, (int, float)):
        price_value = int(value_raw)
    elif isinstance(value_raw, str):
        digits = re.sub(r"\D", "", value_raw)
        if digits:
            try:
                price_value = int(digits)
            except ValueError:
                pass
    if not label:
        if price_value and currency:
            label = f"{price_value} {currency}"
        else:
            label = "Цена не указана"
    if price_obj.get("negotiable"):
        label = f"{label} до торга"
    if price_obj.get("arranged"):
        label = "Договорная"
    return label, price_value, currency


def _usd_estimate(value: int | float, currency: str) -> int | None:
    rate = _USD_RATES.get((currency or "").upper())
    if not rate:
        return None
    usd = int(round(float(value) * rate))
    return usd if usd > 0 else None


def _parse_location(loc: dict) -> str | None:
    if not isinstance(loc, dict):
        return None
    city = (loc.get("city") or {}).get("name") if isinstance(loc.get("city"), dict) else None
    district = (loc.get("district") or {}).get("name") if isinstance(loc.get("district"), dict) else None
    region = (loc.get("region") or {}).get("name") if isinstance(loc.get("region"), dict) else None
    parts = [x for x in (city, district, region) if isinstance(x, str) and x.strip()]
    if not parts:
        return None
    # Dedupe consecutive equal parts (e.g. city == region)
    deduped = []
    for p in parts:
        if not deduped or deduped[-1] != p:
            deduped.append(p)
    return ", ".join(deduped)


def _extract_photo(photos: list) -> str | None:
    if not photos or not isinstance(photos, list):
        return None
    first = photos[0]
    if not isinstance(first, dict):
        return None
    link = (first.get("link") or "").strip()
    if not link:
        return None
    # Template: "...{width}x{height}". Fill in.
    url = link.replace("{width}", str(_PHOTO_W)).replace("{height}", str(_PHOTO_H))
    if not url.startswith("http"):
        return None
    return url


# Simple HTML → plain text. The API returns seller descriptions that
# contain <strong>, <br />, etc. Strip tags, unescape entities, collapse
# runs of whitespace.
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_description(raw: str) -> str:
    if not isinstance(raw, str):
        return ""
    # Convert common block tags to newlines before stripping
    txt = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    txt = re.sub(r"(?i)</p>", "\n\n", txt)
    txt = _HTML_TAG_RE.sub("", txt)
    txt = html_lib.unescape(txt)
    # Collapse whitespace
    txt = re.sub(r"[ \t]{2,}", " ", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


# ---------------------------------------------------------------------------
# Image download with OLX referer
# ---------------------------------------------------------------------------

async def olx_download_image(url: str, proxy: str | None = None) -> bytes | None:
    # olxcdn accepts any olx.* as Referer; .pl hard-coded is safe.
    return await download_image_bytes(
        url, host=_HOST, referer="https://www.olx.pl/", proxy=proxy,
    )
