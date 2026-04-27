"""Vinted (vinted.com / .fr / .de / .pl / …) marketplace parser.

The Vinted catalog page is fully client-rendered — server HTML has zero
items. Real data lives at /api/v2/catalog/items, a JSON endpoint that's
public-but-cookie-gated: every browser session gets a `datadome` cookie
when the homepage loads, and the API only answers if that cookie is
present. We mirror that flow with cloudscraper:

  1. GET https://www.vinted.com/  → datadome cookie lands in the session
  2. GET https://www.vinted.com/api/v2/catalog/items?search_text=...
     → JSON with up to ~50 items (we cap per_page=50)

Catalog items don't expose a `created_at`/`posted_at`. We use the
main photo's `high_resolution.timestamp` (unix seconds) as a publish
proxy — it lines up with item creation in practice and only drifts
if the seller swaps photos.

Currency follows Vinted's IP-based localisation (Railway → USD or EUR
typically). We append a `~N $` hint via a static rate table for the
nine currencies we see most.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import parse_qs, urlencode, urlparse

import orjson

from .base import SearchItem
from .common import (
    download_image_bytes,
    get_cloudscraper,
    global_request_lock,
    invalidate_session,
    proxies_dict,
)

logger = logging.getLogger(__name__)

_HOST = "vinted"
# All TLDs Vinted operates on, plus the "common" /com root.
_VINTED_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?vinted\.(?:com|fr|de|es|it|nl|pl|cz|sk|"
    r"co\.uk|at|be|hu|lt|lv|ro|pt|fi|se|dk|gr|lu|ie)/",
    re.IGNORECASE,
)

_PER_PAGE = 50

# Rough USD rates for the currencies Vinted commonly returns.
# April 2026 ballpark — refresh quarterly.
_USD_RATES: dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "PLN": 0.25,
    "CZK": 0.044,
    "SEK": 0.094,
    "DKK": 0.145,
    "HUF": 0.0028,
    "RON": 0.22,
}

_CURRENCY_SYMBOL: dict[str, str] = {
    "USD": "$", "EUR": "€", "GBP": "£",
    "PLN": "zł", "CZK": "Kč", "SEK": "kr", "DKK": "kr",
    "HUF": "Ft", "RON": "lei",
}

# Per-item enrichment from the full /items/{id} HTML:
#   - ancestors: the breadcrumb chain (gender + category subtree),
#     used for the strict-catalog filter. Server-renders as
#     /catalog/<id>-<slug>?referrer=item-crumbs anchors.
#   - description: from the JSON-LD <script> block (~80 KB into HTML).
#   - location: from the React-stream user_info block (~2.1 MB into HTML).
#
# We cache the whole bundle per item-id with a 24 h TTL so the
# steady-state monitor only pays the page fetch on genuinely new IDs.
_BREADCRUMB_RE = re.compile(r"/catalog/(\d+)-[a-z0-9-]+\?referrer=item-crumbs")
_LD_JSON_RE = re.compile(
    r'<script\s+type="application/ld\+json"\s*>([\s\S]*?)</script>',
    re.IGNORECASE,
)
# user_info location entry. Vinted's React-stream emits the user_info
# block JSON-encoded inside a JS string, so the on-the-wire bytes look
# like `\"text\":\"Vila Nova de Gaia, Portugal\",\"key\":\"location\"`
# (backslash + quote pairs around the keys/values). The raw-string
# regex below matches exactly those literal bytes.
# Two key orders observed in the wild — JSON.stringify in different
# Vinted code paths emits them in either order. Both groups capture
# the city,country string; whichever group matched, we consume.
_LOCATION_RE = re.compile(
    r'\\"text\\":\\"([^"]+?)\\",\\"key\\":\\"location\\"'
    r'|\\"key\\":\\"location\\",\\"text\\":\\"([^"]+?)\\"'
)

# Fallback patterns — some items omit the user_info "location" entry
# but still ship the seller's city/country in separate React-stream
# fields. We stitch them together as "<city>, <country>" when present.
_FALLBACK_CITY_RE = re.compile(r'\\"city\\":\\"([^"\\]+)\\"')
_FALLBACK_COUNTRY_RE = re.compile(r'\\"country_title_local\\":\\"([^"\\]+)\\"')
_ENRICH_CACHE: dict[int, tuple[dict, float]] = {}
_ENRICH_TTL = 24 * 3600.0
# Memory cap — long-running prod accumulates entries faster than the
# 24h TTL decay. Drop the oldest half when we hit the ceiling.
_ENRICH_CACHE_MAX = 5000
# Hard cap on per-cycle item-page fetches so a fresh seed doesn't
# stampede DataDome (or blow the request budget). At 1.5s spacing
# this caps the verify pass at ~45s per cycle — under the 60s tick.
# Cache-hit items don't count towards this cap, so steady state on a
# stable URL is essentially unbounded.
_MAX_ENRICH_PER_CYCLE = 30

# Search-meaningful query keys. We require at least one to be present
# in the user URL so we don't flood with the whole catalogue.
_SEARCH_FILTER_KEYS = (
    "search_text", "catalog[]", "catalog_ids",
    "brand_ids[]", "brand_ids",
    "color_ids[]", "color_ids",
    "size_ids[]", "size_ids",
    "material_ids[]", "material_ids",
    "status_ids[]", "status_ids",
    "video_game_rating_ids[]", "video_game_rating_ids",
    "currency", "price_from", "price_to",
)


class VintedSource:
    name = "vinted"

    def matches(self, url: str) -> bool:
        return bool(_VINTED_URL_RE.search(url or ""))

    async def fetch(
        self, url: str, proxy: str | None, max_retries: int = 3,
    ) -> list[SearchItem] | None:
        async with global_request_lock():
            try:
                return await _fetch_inner(url, proxy, max_retries)
            finally:
                import random
                cooldown = random.uniform(3.0, 7.0)
                logger.info("[vinted] post-request cooldown %.1fs", cooldown)
                await asyncio.sleep(cooldown)


# ---------------------------------------------------------------------------
# Fetch pipeline
# ---------------------------------------------------------------------------

async def _fetch_inner(url: str, proxy: str | None, max_retries: int):
    api_url = _build_api_url(url)
    if api_url is None:
        return None
    for attempt in range(max_retries):
        items, blocked = await _fetch_api(api_url, url, proxy)
        if items is not None:
            logger.info("[vinted] fetched %d items for %s", len(items), url[:80])
            return items
        if not blocked:
            return None
        logger.warning(
            "[vinted] blocked (attempt %d/%d), invalidating session",
            attempt + 1, max_retries,
        )
        invalidate_session(_HOST)
        await asyncio.sleep(5)
    logger.error("[vinted] all %d attempts blocked", max_retries)
    return None


async def _fetch_api(api_url: str, user_url: str, proxy: str | None):
    loop = asyncio.get_running_loop()
    resp_data = await loop.run_in_executor(
        None, lambda: _fetch_api_sync(api_url, user_url, proxy),
    )
    if resp_data is None:
        return None, False
    status, body = resp_data
    if status in (403, 429):
        logger.warning("[vinted] BLOCKED %d", status)
        return None, True
    if status != 200:
        logger.debug("[vinted] HTTP %d", status)
        return None, False
    try:
        data = orjson.loads(body)
    except Exception as e:
        logger.warning("[vinted] JSON decode err: %s", str(e)[:80])
        return None, False

    items = _parse_response(data)
    if items is None:
        return None, False

    # Strict catalog filter: Vinted's `catalog[]=...` filter is loose
    # — sellers categorize themselves and items leak across genders.
    # If the user URL named a specific catalog (e.g. 2050 = men's
    # clothing), enrich each item with its own breadcrumb chain and
    # drop the ones outside that subtree.
    target = _extract_target_catalogs(user_url)
    if target:
        before = len(items)
        origin = _origin_for(user_url) or "https://www.vinted.com"
        items, dropped_outside, failures, unverified = await _filter_by_catalog(
            items, target, origin, proxy,
        )
        logger.info(
            "[vinted] strict catalog %s: %d → %d kept "
            "(dropped %d outside | %d enrich-fail | %d unverified)",
            sorted(target), before, len(items),
            dropped_outside, failures, unverified,
        )
    return items, False


def _fetch_api_sync(api_url: str, user_url: str, proxy: str | None):
    try:
        origin = _origin_for(user_url) or "https://www.vinted.com"
        # Warmup the homepage so the datadome cookie lands in the
        # cloudscraper session before we hit the JSON endpoint.
        s = get_cloudscraper(_HOST, warmup_urls=[origin + "/"], proxy=proxy)
        proxies = proxies_dict(proxy)
        headers = {
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": origin + "/",
        }
        logger.info("[vinted] API REQUEST %s", api_url[:200])
        resp = s.get(api_url, proxies=proxies, timeout=30, headers=headers)
        logger.info(
            "[vinted] API status=%d, size=%d",
            resp.status_code, len(resp.text),
        )
        return resp.status_code, resp.text
    except Exception as e:
        logger.debug("[vinted] sync fetch err: %s", e)
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
# URL → API URL translation
# ---------------------------------------------------------------------------

def _build_api_url(user_url: str) -> str | None:
    p = urlparse(user_url)
    qs = parse_qs(p.query or "", keep_blank_values=True)

    # Reject empty searches — they'd return the whole catalogue and
    # spam the user with thousands of unrelated items.
    has_filter = any(qs.get(k) for k in _SEARCH_FILTER_KEYS)
    if not has_filter:
        logger.warning("[vinted] URL has no recognisable search filter: %s",
                       user_url[:120])
        return None

    # Force newest-first + first page + bounded batch size. Whatever
    # `order=` / `page=` / `per_page=` the user has gets overwritten.
    qs["order"] = ["newest_first"]
    qs["page"] = ["1"]
    qs["per_page"] = [str(_PER_PAGE)]

    origin = _origin_for(user_url) or "https://www.vinted.com"
    return f"{origin}/api/v2/catalog/items?{urlencode(qs, doseq=True)}"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_response(data: dict) -> list[SearchItem] | None:
    raw = data.get("items")
    if not isinstance(raw, list):
        logger.warning("[vinted] response missing items[] array")
        return None
    pagination = data.get("pagination") or {}
    logger.info(
        "[vinted] catalog: %d returned, total=%s",
        len(raw), pagination.get("total_entries"),
    )

    items: list[SearchItem] = []
    skipped = 0
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if entry.get("promoted"):
            skipped += 1
            continue
        if entry.get("is_visible") is False:
            skipped += 1
            continue
        try:
            items.append(_parse_item(entry))
        except Exception as e:
            logger.debug("[vinted] parse err: %s", e)

    if items:
        total_cnt = len(items)
        with_image = sum(1 for i in items if i.image_url)
        with_price = sum(1 for i in items if i.price_value)
        with_ts = sum(1 for i in items if i.published_timestamp)
        logger.info(
            "[vinted] completeness: image=%d/%d, price=%d/%d, date=%d/%d "
            "(skipped %d promoted/hidden)",
            with_image, total_cnt, with_price, total_cnt,
            with_ts, total_cnt, skipped,
        )
    return items


# ---------------------------------------------------------------------------
# Strict catalog filter — Vinted's server-side `catalog[]` filter is
# loose (sellers mis-categorize, items leak across genders). To get a
# clean men's-only / women's-only stream we fetch each item page's
# breadcrumb, extract its ancestor chain, and drop items whose chain
# doesn't intersect the user's target catalogs.
# ---------------------------------------------------------------------------

def _extract_target_catalogs(user_url: str) -> frozenset[int]:
    """Return the set of catalog IDs the user explicitly asked for in
    `catalog[]=...` (or `catalog_ids[]=...`). Empty frozenset means no
    strict filter — the parser then accepts whatever the search API
    returns."""
    try:
        p = urlparse(user_url)
    except Exception:
        return frozenset()
    qs = parse_qs(p.query or "")
    out: set[int] = set()
    for key in ("catalog[]", "catalog_ids[]", "catalog_ids", "catalog"):
        for raw in qs.get(key, []):
            for token in raw.split(","):
                token = token.strip()
                if token.isdigit():
                    out.add(int(token))
    return frozenset(out)


def _fetch_item_metadata_sync(
    item_id: int, origin: str, proxy: str | None,
) -> dict | None:
    """GET /items/{id} (full HTML, no Range) and extract everything we
    can in one pass: breadcrumb ancestors, JSON-LD description, and
    the seller's city/country from the React-stream user_info block.

    Returns a dict like
        {"ancestors": frozenset[int], "description": str | None,
         "location": str | None}
    or None on any fetch failure. Cached per item-id (24 h TTL)."""
    cached = _ENRICH_CACHE.get(item_id)
    now = time.time()
    if cached and (now - cached[1]) < _ENRICH_TTL:
        return cached[0]
    try:
        s = get_cloudscraper(_HOST, warmup_urls=[origin + "/"], proxy=proxy)
        proxies = proxies_dict(proxy)
        url = f"{origin}/items/{item_id}"
        # No Range header — the seller location lives ~2.1 MB into the
        # 2.3 MB item HTML, in a React-stream user_info block. Without
        # the full page we'd lose location entirely.
        headers = {
            "Accept": "text/html",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": origin + "/",
        }
        resp = s.get(url, proxies=proxies, timeout=30, headers=headers)
        if resp.status_code not in (200, 206):
            logger.debug("[vinted] item %d page HTTP %d", item_id, resp.status_code)
            return None
        html = resp.text or ""

        # Ancestors live near the top (byte ~73 KB) — slice for the regex
        # to keep work bounded.
        ancestors_html = html[:200_000]
        ancestor_ids = {int(m) for m in _BREADCRUMB_RE.findall(ancestors_html)}
        if not ancestor_ids:
            # Page didn't render a breadcrumb (deleted item, A/B variant,
            # interstitial, …). Treat as failure so the strict-filter
            # caller fails-closed.
            return None
        ancestors = frozenset(ancestor_ids)

        loc = _extract_location_from_html(html)
        if not loc:
            # Help diagnose location-regex regressions when the React
            # stream layout changes — log once with the HTML size so we
            # can tell apart "page truncated" from "user_info absent".
            logger.info(
                "[vinted] item %d: location not found (html=%d KB)",
                item_id, len(html) // 1024,
            )
        meta = {
            "ancestors": ancestors,
            "description": _extract_description_from_html(html),
            "location": loc,
        }
        if len(_ENRICH_CACHE) >= _ENRICH_CACHE_MAX:
            keys = list(_ENRICH_CACHE.keys())[: _ENRICH_CACHE_MAX // 2]
            for k in keys:
                _ENRICH_CACHE.pop(k, None)
        _ENRICH_CACHE[item_id] = (meta, now)
        return meta
    except Exception as e:
        logger.debug("[vinted] item %d enrichment err: %s",
                     item_id, str(e)[:100])
        return None


def _extract_description_from_html(html: str) -> str | None:
    """Pull the seller's description out of the JSON-LD <script> block
    Vinted server-renders for SEO. Falls back to the og:description
    meta tag for items where JSON-LD is missing or malformed.

    HTML entities (`&amp;`, `&quot;`, `&#39;`) are decoded so the bot
    doesn't ship raw entities in the notification card."""
    import html as _html

    # JSON-LD lives in the first ~80 KB; scan only that window.
    head = html[:120_000]
    for m in _LD_JSON_RE.finditer(head):
        body = (m.group(1) or "").strip()
        if not body:
            continue
        try:
            data = orjson.loads(body)
        except Exception:
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            desc = data.get("description")
            if isinstance(desc, str) and desc.strip():
                return _html.unescape(desc.strip())
    # Fallback: <meta property="og:description" content="..."/>
    m = re.search(
        r'<meta\s+property="og:description"\s+content="([^"]{1,2000})"',
        head,
    )
    if m:
        # The og:description starts with the title; strip the title
        # prefix so we don't show the title twice in the notification.
        og = _html.unescape(m.group(1).strip())
        if " - " in og:
            return og.split(" - ", 1)[1].strip() or None
        return og
    return None


def _extract_location_from_html(html: str) -> str | None:
    """Pull `<city>, <country>` out of the React-stream JSON near the
    end of the item HTML.

    Tries three sources in order:
      1. user_info `text:<X>,key:location` block (preferred — already
         pre-formatted as "City, Country" in the seller's locale).
      2. Separate `city` + `country_title_local` fields, stitched.
      3. `city` alone if country isn't there.
    """
    import json as _json

    def _decode(raw: str) -> str | None:
        s = raw.strip()
        if not s:
            return None
        # Captured value may carry JSON `\uXXXX` escapes for non-ASCII
        # cities. Round-trip through json.loads to decode them, but
        # fall back to the raw value if the wrapper makes it un-parseable.
        try:
            decoded = _json.loads(f'"{s}"')
            if isinstance(decoded, str) and decoded.strip():
                return decoded.strip()
        except Exception:
            pass
        return s

    m = _LOCATION_RE.search(html)
    if m:
        raw = m.group(1) or m.group(2) or ""
        out = _decode(raw)
        if out:
            return out

    # Fallback: stitch city + country from separate fields.
    city_m = _FALLBACK_CITY_RE.search(html)
    country_m = _FALLBACK_COUNTRY_RE.search(html)
    city = _decode(city_m.group(1)) if city_m else None
    country = _decode(country_m.group(1)) if country_m else None
    if city and country:
        return f"{city}, {country}"
    if city:
        return city
    if country:
        return country
    return None


def _metadata_from_cache(item_id: int) -> dict | None:
    cached = _ENRICH_CACHE.get(item_id)
    if cached and (time.time() - cached[1]) < _ENRICH_TTL:
        return cached[0]
    return None


def _apply_enrichment(item: SearchItem, meta: dict) -> None:
    """Copy the description / location we scraped off the item HTML
    onto the SearchItem (which the API response left empty)."""
    desc = meta.get("description")
    if isinstance(desc, str) and desc.strip() and not item.description:
        item.description = desc.strip()
    loc = meta.get("location")
    if isinstance(loc, str) and loc.strip() and not item.location:
        item.location = loc.strip()


async def _filter_by_catalog(
    items: list[SearchItem], target_catalogs: frozenset[int],
    origin: str, proxy: str | None,
) -> tuple[list[SearchItem], int, int, int]:
    """Strict catalog filter + per-item description/location enrichment.

    Returns (kept, dropped_outside, fetch_failures, dropped_unverified).

    For each item we fetch /items/{id} HTML once, scrape three things
    in one pass: breadcrumb ancestor chain (drives the filter),
    JSON-LD description, and the seller location from the React-stream
    user_info block. Kept items get description + location patched
    onto their SearchItem.

    Fail-CLOSED: items whose ancestors couldn't be determined (network
    error, DataDome 429, redirect, empty crumbs) are DROPPED. Empirical
    research (research/VINTED_LIVE_ANALYSIS.md) showed that
    `/api/v2/catalog/items?catalog[]=N` is a decorative filter that
    leaks ~80% women's/kids' items into a men's-clothing search, so any
    item we can't positively verify must be assumed leaky.

    Cache-hit items are evaluated synchronously (no sleep) so steady
    state stays cheap; only cache-miss items pay the 1.5s rate-limit
    spacing required by Vinted's per-IP throttle on /items/{id}."""
    if not target_catalogs:
        return items, 0, 0, 0
    loop = asyncio.get_running_loop()
    kept: list[SearchItem] = []
    dropped_outside = 0
    failures = 0
    dropped_unverified = 0
    enriched = 0
    for item in items:
        try:
            item_id = int(item.external_id)
        except (TypeError, ValueError):
            dropped_unverified += 1
            continue

        meta = _metadata_from_cache(item_id)
        if meta is None:
            # Cache miss — pay HTTP. Hard cap so first-scan seed doesn't
            # fire 50 sequential page loads and trip DataDome's per-IP
            # throttle.
            if enriched >= _MAX_ENRICH_PER_CYCLE:
                dropped_unverified += 1
                continue
            meta = await loop.run_in_executor(
                None,
                lambda i=item_id: _fetch_item_metadata_sync(i, origin, proxy),
            )
            enriched += 1
            if meta is None:
                failures += 1
                # Fail-CLOSED: drop unverified rather than risk a leak.
                continue
            # 1.5s spacing held under sustained 30-item enrichment in
            # research; faster spacing tripped 429 within ~4 requests.
            await asyncio.sleep(1.5)

        ancestors = meta.get("ancestors") or frozenset()
        if ancestors & target_catalogs:
            _apply_enrichment(item, meta)
            kept.append(item)
        else:
            dropped_outside += 1

    # Post-enrichment completeness — surfaces how many kept items
    # actually received a location / description after the per-item
    # HTML scrape, so a regex regression is visible in Railway logs.
    if kept:
        with_loc = sum(1 for i in kept if i.location)
        with_desc = sum(1 for i in kept if i.description)
        logger.info(
            "[vinted] enriched %d kept: location=%d/%d, description=%d/%d",
            len(kept), with_loc, len(kept), with_desc, len(kept),
        )
    return kept, dropped_outside, failures, dropped_unverified


def _parse_item(entry: dict) -> SearchItem:
    ext_id = str(entry.get("id") or "")
    base_title = (entry.get("title") or "").strip()
    brand = (entry.get("brand_title") or "").strip() or None
    size = (entry.get("size_title") or "").strip() or None
    status = (entry.get("status") or "").strip() or None

    # Size and condition are kept on dedicated fields so the renderer
    # can append them in the user's language ("(M, Хорошее)") AFTER
    # translation. Earlier we folded them into `title` and Google
    # Translate left mixed-language strings half-translated
    # ("(Nuevo sin etiquetas)" inside an otherwise-Russian title).
    if size and size.lower() in base_title.lower():
        size = None
    if status and status.lower() in base_title.lower():
        status = None

    item_url = (entry.get("url") or "").strip()
    if not item_url:
        path = (entry.get("path") or "").strip()
        if path:
            item_url = "https://www.vinted.com" + path

    price_str, price_value, price_currency = _parse_price(entry.get("price"))

    image_url = _extract_image(entry)

    seller = None
    user = entry.get("user") or {}
    if isinstance(user, dict):
        login = (user.get("login") or "").strip()
        if login:
            seller = login

    ts = _extract_timestamp(entry)

    return SearchItem(
        source="vinted",
        external_id=ext_id,
        title=base_title,
        price=price_str,
        price_value=price_value,
        url=item_url,
        image_url=image_url,
        location=None,            # not in catalog response
        description=None,         # not in catalog response
        seller_name=seller,
        published_timestamp=ts,
        brand=brand,
        condition=status,
        size=size,
        currency=price_currency,
    )


def _parse_price(price) -> tuple[str, int | None, str | None]:
    """Parse Vinted price block.

    Returns (label, value_int, currency_code). The label is the native
    rendering ("14 €"); the user-currency estimate is appended later by
    the renderer using `parsers.currency.format_with_estimate`.
    """
    if not isinstance(price, dict):
        return "Цена не указана", None, None
    raw_amt = price.get("amount")
    currency = (price.get("currency_code") or "").upper() or None
    if raw_amt is None:
        return "Цена не указана", None, None
    try:
        # Vinted ships amounts as strings like "115.0"
        amt_f = float(raw_amt)
    except (TypeError, ValueError):
        return "Цена не указана", None, None
    value = int(round(amt_f))
    if value <= 0:
        return "Цена не указана", None, None
    sym = _CURRENCY_SYMBOL.get(currency, currency or "")
    label = f"{value} {sym}".strip()
    return label, value, currency


def _extract_image(entry: dict) -> str | None:
    photos = entry.get("photos")
    if isinstance(photos, list) and photos:
        first = photos[0]
        if isinstance(first, dict):
            url = first.get("url") or first.get("full_size_url")
            if isinstance(url, str) and url.startswith("http"):
                return url
    photo = entry.get("photo")
    if isinstance(photo, dict):
        url = photo.get("url") or photo.get("full_size_url")
        if isinstance(url, str) and url.startswith("http"):
            return url
    return None


def _extract_timestamp(entry: dict) -> int | None:
    """Catalog responses don't carry created_at; the main photo's
    high_resolution.timestamp is a close-enough publish proxy."""
    candidates = []
    photos = entry.get("photos")
    if isinstance(photos, list) and photos:
        candidates.append(photos[0])
    if isinstance(entry.get("photo"), dict):
        candidates.append(entry["photo"])
    for c in candidates:
        hr = c.get("high_resolution") if isinstance(c, dict) else None
        if isinstance(hr, dict):
            ts = hr.get("timestamp")
            if isinstance(ts, (int, float)) and ts > 1_000_000_000:
                return int(ts)
    return None


# ---------------------------------------------------------------------------
# Image download with Vinted referer
# ---------------------------------------------------------------------------

async def vinted_download_image(url: str, proxy: str | None = None) -> bytes | None:
    return await download_image_bytes(
        url, host=_HOST, referer="https://www.vinted.com/", proxy=proxy,
    )
