"""Mercari JP (jp.mercari.com) marketplace parser.

The Mercari listing page is entirely client-rendered — the raw server
HTML has zero item anchors, so there's no DOM path available. The JSON
API at api.mercari.jp/v2/entities:search requires a DPoP token (JWT
signed with a per-request-rotated ES256 keypair) + X-Platform / client
version headers. The `mercapi` library handles all of that for us.

Quirks learnt the hard way:
  - SORT_CREATED_TIME:DESC is "best-effort" according to mercapi's own
    docstring; the API returns a mix of regular and ITEM_TYPE_BEYOND
    (shop) items whose relative ordering is opaque. We therefore
    filter to ITEM_TYPE_MERCARI and sort by `created` descending on
    the client side.
  - mercapi deserializes timestamps with datetime.fromtimestamp(ts)
    (naive, system-local). Calling .timestamp() on the naive dt
    round-trips back to the correct unix UTC seconds on any host — so
    scheduler's MSK formatter displays the right wall-clock time.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from .base import SearchItem
from .common import host_in_allowlist

logger = logging.getLogger(__name__)

# Hostname allowlist (see common.host_in_allowlist for SSRF rationale).
_MERCARI_HOSTS = frozenset({
    "mercari.com", "www.mercari.com",
    "mercari.jp", "www.mercari.jp", "jp.mercari.com",
})

# Rough JPY → USD rate (April 2026). Update quarterly.
_JPY_TO_USD = 0.0065

# Max items returned from one search call (mercapi defaults to 120).
_KEEP_TOP = 50

# Per-item enrichment cache: id_ → {ts, location, description, seller_name}.
# Mercari's search API doesn't return location / description / seller —
# those require a per-item m.item(id) call. We enrich items inline,
# cache for 24 h, and stagger fetches by 0.2 s to play nice with
# Mercari's anti-bot. Steady state (no new items) hits 0 fetches.
_ENRICH_CACHE: dict[str, dict] = {}
_ENRICH_TTL = 24 * 3600
_ENRICH_DELAY = 0.2
_ENRICH_TIMEOUT = 8.0
# Long-running prod with thousands of items per day will accumulate
# entries faster than the 24h TTL natural decay; cap the dict and
# evict the oldest half when we hit the ceiling so memory doesn't
# grow unbounded over the lifetime of a Railway deploy.
_ENRICH_CACHE_MAX = 5000


class MercariSource:
    name = "mercari"

    def matches(self, url: str) -> bool:
        return host_in_allowlist(url, _MERCARI_HOSTS)

    async def fetch(
        self, url: str, proxy: str | None, max_retries: int = 3,
    ) -> list[SearchItem] | None:
        # mercapi manages its own httpx session and DPoP keypair. We
        # don't share cloudscraper, don't rotate IPs — if DataDome
        # blocks Railway's outbound IP, errors will surface here and
        # the scheduler will pause the sub after 3 consecutive fails.
        try:
            return await asyncio.wait_for(_fetch_inner(url), timeout=30)
        except asyncio.TimeoutError:
            logger.warning("[mercari] overall timeout for %s", url[:100])
            return None
        except Exception:
            logger.exception("[mercari] unexpected error")
            return None


async def _fetch_inner(url: str) -> list[SearchItem] | None:
    try:
        from mercapi import Mercapi
        from mercapi.requests.search import SearchRequestData
    except ImportError:
        logger.warning("[mercari] mercapi not installed")
        return None

    filters = _extract_filters(url)
    if filters is None:
        logger.warning(
            "[mercari] URL has no keyword / category_id / brand_id: %s",
            url[:120],
        )
        return None

    m = Mercapi()
    try:
        results = await m.search(
            filters["keyword"],
            categories=filters["categories"],
            brands=filters["brands"],
            item_conditions=filters["item_conditions"],
            price_min=filters["price_min"],
            price_max=filters["price_max"],
            sort_by=SearchRequestData.SortBy.SORT_CREATED_TIME,
            sort_order=SearchRequestData.SortOrder.ORDER_DESC,
            status=[SearchRequestData.Status.STATUS_ON_SALE],
        )
    except Exception as e:
        logger.warning(
            "[mercari] search error (filters=%s): %s",
            _log_filter_summary(filters), str(e)[:180],
        )
        return None

    raw_items = list(results.items or [])

    # Shop ("Beyond") items are ID-prefixed weirdly and their sort is
    # opaque; keep only regular C2C listings (m-prefix / ITEM_TYPE_MERCARI)
    # so the newest-first monitor actually sees newest-first.
    regular = [
        it for it in raw_items
        if getattr(it, "item_type", "") == "ITEM_TYPE_MERCARI"
        and str(getattr(it, "id_", "") or "").startswith("m")
        and getattr(it, "created", None) is not None
    ]
    # Manual sort — API's DESC is "best-effort" and ships things out of
    # order in practice.
    regular.sort(key=lambda x: x.created, reverse=True)
    regular = regular[:_KEEP_TOP]

    logger.info(
        "[mercari] filters=%s → %d raw, %d regular-kept (skipped %d shops)",
        _log_filter_summary(filters),
        len(raw_items), len(regular), len(raw_items) - len(regular),
    )

    items: list[SearchItem] = []
    for raw in regular:
        try:
            items.append(_parse_item(raw))
        except Exception as e:
            logger.debug("[mercari] parse err: %s", e)

    # Enrich with location / description / seller_name from full item
    # details. Mercari search results omit those, but the user-facing
    # card needs them — cached for 24h so steady-state cycles don't
    # repeat the lookup.
    if items:
        await _enrich_items(items, m)

        total_cnt = len(items)
        with_image = sum(1 for i in items if i.image_url)
        with_ts = sum(1 for i in items if i.published_timestamp)
        with_loc = sum(1 for i in items if i.location)
        with_desc = sum(1 for i in items if i.description)
        logger.info(
            "[mercari] completeness: image=%d/%d, date=%d/%d, "
            "location=%d/%d, description=%d/%d",
            with_image, total_cnt, with_ts, total_cnt,
            with_loc, total_cnt, with_desc, total_cnt,
        )
    return items


async def _enrich_items(items: list[SearchItem], m) -> None:
    """Populate item.location / .description / .seller_name from
    per-item m.item(id) lookups. Mutates items in place. Failures are
    silent — the item just keeps its original (empty) field and the
    notification falls back to «—»."""
    now = time.time()
    for it in items:
        if not it.external_id:
            continue
        cached = _ENRICH_CACHE.get(it.external_id)
        if cached and (now - cached["ts"]) < _ENRICH_TTL:
            it.location = cached.get("location") or it.location
            it.description = cached.get("description") or it.description
            it.seller_name = cached.get("seller_name") or it.seller_name
            continue

        try:
            full = await asyncio.wait_for(
                m.item(it.external_id), timeout=_ENRICH_TIMEOUT,
            )
        except Exception as e:
            logger.debug(
                "[mercari] enrich %s err: %s", it.external_id, str(e)[:120],
            )
            await asyncio.sleep(_ENRICH_DELAY)
            continue

        loc_obj = getattr(full, "shipping_from_area", None)
        loc_name = (getattr(loc_obj, "name", None) or "").strip() if loc_obj else ""
        seller_obj = getattr(full, "seller", None)
        seller_name = (
            (getattr(seller_obj, "name", None) or "").strip() if seller_obj else ""
        )
        desc = (getattr(full, "description", None) or "").strip()

        loc = loc_name or None
        desc_v = desc or None
        seller_v = seller_name or None

        if loc:
            it.location = loc
        if desc_v:
            it.description = desc_v
        if seller_v:
            it.seller_name = seller_v

        if len(_ENRICH_CACHE) >= _ENRICH_CACHE_MAX:
            # Crude LRU: drop the oldest half. CPython 3.7+ dicts
            # preserve insertion order, which approximates LRU well
            # enough for our use case.
            keys = list(_ENRICH_CACHE.keys())[: _ENRICH_CACHE_MAX // 2]
            for k in keys:
                _ENRICH_CACHE.pop(k, None)
        _ENRICH_CACHE[it.external_id] = {
            "ts": now,
            "location": loc,
            "description": desc_v,
            "seller_name": seller_v,
        }
        await asyncio.sleep(_ENRICH_DELAY)


_KEYWORD_KEYS = ("keyword", "query", "q")
_INT_CSV_RE = re.compile(r"\d+")


def _extract_keyword(url: str) -> str | None:
    """Back-compat helper — legacy callers expect this. See _extract_filters
    for the real thing."""
    try:
        p = urlparse(url)
    except Exception:
        return None
    qs = parse_qs(p.query or "")
    for k in _KEYWORD_KEYS:
        v = (qs.get(k) or [""])[0].strip()
        if v:
            return v
    return None


def _extract_filters(url: str) -> dict | None:
    """Pull every Mercari search filter we care about out of the user URL.

    Mercari JP's web search supports lots of query params; the common ones
    we map through to mercapi are keyword / category / brand / price /
    item condition. At least one of {keyword, category_id, brand_id}
    must be present — otherwise the search is meaningless (returns the
    whole catalogue).

    Returns None if nothing was parseable; otherwise a dict safe to
    splat into Mercapi.search().
    """
    try:
        p = urlparse(url)
    except Exception:
        return None
    qs = parse_qs(p.query or "")

    keyword = ""
    for k in _KEYWORD_KEYS:
        v = (qs.get(k) or [""])[0].strip()
        if v:
            keyword = v
            break

    def _csv_ints(*keys: str) -> list[int]:
        out: list[int] = []
        for k in keys:
            for raw in qs.get(k, []):
                out.extend(int(m.group()) for m in _INT_CSV_RE.finditer(raw))
        # Dedupe preserving first-seen order
        seen: set[int] = set()
        uniq: list[int] = []
        for x in out:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq

    categories = _csv_ints("category_id", "category_ids", "categories")
    brands = _csv_ints("brand_id", "brand_ids", "brands")
    item_conditions = _csv_ints("item_condition_id", "item_conditions")

    def _int_param(*keys: str) -> int | None:
        for k in keys:
            v = (qs.get(k) or [""])[0].strip()
            if v.isdigit():
                return int(v)
        return None

    price_min = _int_param("price_min", "priceMin")
    price_max = _int_param("price_max", "priceMax")

    if not keyword and not categories and not brands:
        return None

    return {
        "keyword": keyword,
        "categories": categories,
        "brands": brands,
        "item_conditions": item_conditions,
        "price_min": price_min,
        "price_max": price_max,
    }


def _log_filter_summary(filters: dict) -> str:
    """One-line filter summary for log lines — keeps log grep-able."""
    parts = []
    if filters.get("keyword"):
        parts.append(f"kw={filters['keyword']!r}")
    if filters.get("categories"):
        parts.append(f"cat={filters['categories']}")
    if filters.get("brands"):
        parts.append(f"brand={filters['brands']}")
    if filters.get("item_conditions"):
        parts.append(f"cond={filters['item_conditions']}")
    if filters.get("price_min") is not None:
        parts.append(f"min={filters['price_min']}")
    if filters.get("price_max") is not None:
        parts.append(f"max={filters['price_max']}")
    return "{" + ", ".join(parts) + "}" if parts else "{}"


def _parse_item(it) -> SearchItem:
    ext_id = str(getattr(it, "id_", "") or "")
    name = getattr(it, "name", "") or ""
    title = name.strip()

    price_value = None
    is_no_price = bool(getattr(it, "is_no_price", False))
    raw_price = getattr(it, "price", None)
    if raw_price is not None and not is_no_price:
        try:
            price_value = int(raw_price)
            if price_value <= 0:
                price_value = None
        except (TypeError, ValueError):
            price_value = None

    price_str = _format_jpy_price(price_value)

    image_url = None
    thumbs = getattr(it, "thumbnails", None) or []
    if thumbs and isinstance(thumbs[0], str) and thumbs[0].startswith("http"):
        image_url = thumbs[0]

    item_url = f"https://jp.mercari.com/item/{ext_id}"

    ts: int | None = None
    created = getattr(it, "created", None)
    updated = getattr(it, "updated", None)
    # Prefer `updated` — a just-bumped listing reads as "newer" and
    # that's what the user wants to be alerted about.
    for candidate in (updated, created):
        if isinstance(candidate, datetime):
            try:
                ts = int(candidate.timestamp())
                break
            except (OSError, OverflowError, ValueError):
                continue

    return SearchItem(
        source="mercari",
        external_id=ext_id,
        title=title,
        price=price_str,
        price_value=price_value,
        url=item_url,
        image_url=image_url,
        location=None,        # search results carry no region
        description=None,     # requires a per-item full_item() fetch
        seller_name=None,     # ditto — Profile lookup by seller_id
        published_timestamp=ts,
    )


def _format_jpy_price(value: int | None) -> str:
    if not value or value <= 0:
        return "Цена не указана"
    yen_str = f"{value:,}".replace(",", " ") + " ¥"
    usd = int(round(value * _JPY_TO_USD))
    if usd > 0:
        return f"{yen_str} (~{usd} $)"
    return yen_str
