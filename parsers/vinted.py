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
    return _parse_response(data), False


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


def _parse_item(entry: dict) -> SearchItem:
    ext_id = str(entry.get("id") or "")
    base_title = (entry.get("title") or "").strip()
    brand = (entry.get("brand_title") or "").strip()
    size = (entry.get("size_title") or "").strip()
    status = (entry.get("status") or "").strip()

    # Build a rich title — Vinted's bare title is often something like
    # "Iphone case", which is way too generic on its own. Brand + size +
    # condition give the reader real signal at a glance.
    extras = [x for x in (brand, size, status) if x]
    title = base_title
    if extras:
        # Avoid duplicating brand if it's already in the title (common)
        unique_extras = [
            x for x in extras
            if x.lower() not in base_title.lower()
        ]
        if unique_extras:
            title = f"{base_title} ({', '.join(unique_extras)})"

    item_url = (entry.get("url") or "").strip()
    if not item_url:
        path = (entry.get("path") or "").strip()
        if path:
            item_url = "https://www.vinted.com" + path

    price_str, price_value = _parse_price(entry.get("price"))

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
        title=title,
        price=price_str,
        price_value=price_value,
        url=item_url,
        image_url=image_url,
        location=None,            # not in catalog response
        description=None,         # not in catalog response
        seller_name=seller,
        published_timestamp=ts,
    )


def _parse_price(price) -> tuple[str, int | None]:
    if not isinstance(price, dict):
        return "Цена не указана", None
    raw_amt = price.get("amount")
    currency = (price.get("currency_code") or "").upper()
    if raw_amt is None:
        return "Цена не указана", None
    try:
        # Vinted ships amounts as strings like "115.0"
        amt_f = float(raw_amt)
    except (TypeError, ValueError):
        return "Цена не указана", None
    value = int(round(amt_f))
    if value <= 0:
        return "Цена не указана", None
    sym = _CURRENCY_SYMBOL.get(currency, currency)
    label = f"{value} {sym}".strip()
    if currency != "USD":
        rate = _USD_RATES.get(currency)
        if rate:
            usd = int(round(value * rate))
            if usd > 0:
                label = f"{label} (~{usd} $)"
    return label, value


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
