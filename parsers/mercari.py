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
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from .base import SearchItem

logger = logging.getLogger(__name__)

_MERCARI_URL_RE = re.compile(
    r"https?://(?:www\.|jp\.)?mercari\.(?:com|jp)/",
    re.IGNORECASE,
)

# Rough JPY → USD rate (April 2026). Update quarterly.
_JPY_TO_USD = 0.0065

# Max items returned from one search call (mercapi defaults to 120).
_KEEP_TOP = 50


class MercariSource:
    name = "mercari"

    def matches(self, url: str) -> bool:
        return bool(_MERCARI_URL_RE.search(url or ""))

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

    keyword = _extract_keyword(url)
    if not keyword:
        logger.warning("[mercari] no ?keyword= / ?q= / ?query= in URL: %s",
                       url[:120])
        return None

    m = Mercapi()
    try:
        results = await m.search(
            keyword,
            sort_by=SearchRequestData.SortBy.SORT_CREATED_TIME,
            sort_order=SearchRequestData.SortOrder.ORDER_DESC,
            status=[SearchRequestData.Status.STATUS_ON_SALE],
        )
    except Exception as e:
        logger.warning("[mercari] search error (keyword=%r): %s",
                       keyword, str(e)[:180])
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
        "[mercari] keyword=%r → %d raw, %d regular-kept (skipped %d shops)",
        keyword, len(raw_items), len(regular), len(raw_items) - len(regular),
    )

    items: list[SearchItem] = []
    for raw in regular:
        try:
            items.append(_parse_item(raw))
        except Exception as e:
            logger.debug("[mercari] parse err: %s", e)

    if items:
        total_cnt = len(items)
        with_image = sum(1 for i in items if i.image_url)
        with_ts = sum(1 for i in items if i.published_timestamp)
        logger.info(
            "[mercari] completeness: image=%d/%d, date=%d/%d",
            with_image, total_cnt, with_ts, total_cnt,
        )
    return items


_KEYWORD_KEYS = ("keyword", "query", "q")


def _extract_keyword(url: str) -> str | None:
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
