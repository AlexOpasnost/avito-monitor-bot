"""Fruitsfamily (fruitsfamily.com) — Korean vintage / streetwear / luxury C2C.

Web frontend is React; product data comes from a public GraphQL endpoint
at `web-server.production.fruitsfamily.com/graphql`. No auth needed for
search results. Reverse-engineered from the live page on 2026-04-29:

  POST https://web-server.production.fruitsfamily.com/graphql
  Content-Type: application/json
  Body: {
    "operationName": "SeeProducts",
    "variables": {
      "filter": {
        "gender": "MEN" | "WOMEN",
        "subcategory_ids": [int, ...],   // optional
        "price_min": int, "price_max": int,  // optional
        "brand": str,  // optional
      },
      "sort": "NEW" | "POPULAR",
      "offset": int, "limit": int
    },
    "query": "query SeeProducts(...) { searchProducts(...) {...} }"
  }

ProductFilter does NOT accept free-text keyword — it's category-based
only. Users paste a /search?gender=MEN&subcategoryIds=N URL; we mirror
those parameters into the GraphQL filter.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx

from .base import SearchItem
from .common import MAX_JSON_BYTES, host_in_allowlist, host_request_lock

logger = logging.getLogger(__name__)

_HOST = "fruitsfamily"
_FF_HOSTS = frozenset({
    "fruitsfamily.com", "www.fruitsfamily.com", "m.fruitsfamily.com",
})

_API_URL = "https://web-server.production.fruitsfamily.com/graphql"
_PRODUCT_BASE = "https://fruitsfamily.com/product"
_TIMEOUT = 25.0
_PER_PAGE = 40

# Cap the number of items per cycle so a fresh subscription on a
# fast-moving category doesn't push 100 photos at once. Same logic as
# the other marketplaces.
_KEEP_TOP = 50

# Trimmed query — only the fields we actually use downstream. Keeping
# it minimal makes the wire smaller and means the parser doesn't break
# if Fruitsfamily adds/removes peripheral fields.
_GRAPHQL_QUERY = (
    "query SeeProducts($filter: ProductFilter!, $offset: Int, "
    "$limit: Int, $sort: String) {"
    "  searchProducts(filter: $filter, offset: $offset, limit: $limit, "
    "                 sort: $sort) {"
    "    id createdAt category title brand price status external_url"
    "    resizedSmallImages size condition __typename"
    "  }"
    "}"
)

# Map condition enum values returned by the API to short human strings.
# Translation into the user's language happens later in scheduler's
# _localise_item; here we only normalise the enum to readable text.
_CONDITION_LABELS: dict[str, str] = {
    "NEW_WITH_TAG":   "Новое с биркой",
    "NEW":            "Новое",
    "GOOD_CONDITION": "Хорошее состояние",
    "NORMAL":         "Среднее состояние",
    "BAD":            "Плохое состояние",
    "USED":           "Бывшее в употреблении",
}


class FruitsfamilySource:
    name = "fruitsfamily"

    def matches(self, url: str) -> bool:
        return host_in_allowlist(url, _FF_HOSTS)

    async def fetch(
        self, url: str, proxy: str | None, max_retries: int = 3,
    ) -> list[SearchItem] | None:
        # Per-host lock so N concurrent subs on Fruitsfamily don't
        # turn into N parallel POSTs at the same minute mark.
        # GraphQL endpoint answers in <1s normally, but the hosting
        # provider has been observed rate-limiting bursts.
        async with host_request_lock(_HOST):
            for attempt in range(max_retries):
                try:
                    items = await _fetch_inner(url, proxy)
                    if items is not None:
                        return items
                except Exception as e:
                    logger.warning(
                        "[fruitsfamily] attempt %d/%d failed: %s",
                        attempt + 1, max_retries, str(e)[:120],
                    )
                await asyncio.sleep(2 + attempt * 2)
        return None


async def _fetch_inner(url: str, proxy: str | None) -> list[SearchItem] | None:
    filter_payload = _filter_from_url(url)
    if filter_payload is None:
        logger.info(
            "[fruitsfamily] URL has no recognised filter — skipping: %s",
            url[:100],
        )
        return None

    body = {
        "operationName": "SeeProducts",
        "variables": {
            "filter": filter_payload,
            "sort": "NEW",
            "offset": 0,
            "limit": _PER_PAGE,
        },
        "query": _GRAPHQL_QUERY,
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://fruitsfamily.com",
        "Referer": "https://fruitsfamily.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    proxies = {"http://": proxy, "https://": proxy} if proxy else None
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT,
            proxies=proxies,
            follow_redirects=False,
        ) as client:
            resp = await client.post(_API_URL, json=body, headers=headers)
    except httpx.RequestError as e:
        logger.warning("[fruitsfamily] network err: %s", str(e)[:120])
        return None

    if resp.status_code != 200:
        logger.warning(
            "[fruitsfamily] HTTP %d body=%s",
            resp.status_code, resp.text[:300],
        )
        return None

    # Body-size cap — see common.MAX_JSON_BYTES rationale.
    body_bytes = resp.content
    if len(body_bytes) > MAX_JSON_BYTES:
        logger.warning(
            "[fruitsfamily] response oversized: %d bytes",
            len(body_bytes),
        )
        return None
    try:
        data = resp.json()
    except Exception as e:
        logger.warning("[fruitsfamily] JSON decode err: %s", str(e)[:120])
        return None

    if data.get("errors"):
        logger.warning(
            "[fruitsfamily] GraphQL errors: %s",
            str(data["errors"])[:300],
        )
        return None

    products = (data.get("data") or {}).get("searchProducts") or []
    items: list[SearchItem] = []
    for p in products[:_KEEP_TOP]:
        item = _parse_item(p)
        if item is not None:
            items.append(item)
    logger.info(
        "[fruitsfamily] fetched %d items (filter=%s)",
        len(items), filter_payload,
    )
    return items


def _filter_from_url(url: str) -> dict | None:
    """Translate a /search?gender=...&subcategoryIds=...&... URL into the
    GraphQL filter dict. Returns None when the URL has no parameters
    we know how to map (so we don't accidentally fan out into "all
    products on the site" mode).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    qs = parse_qs(parsed.query, keep_blank_values=False)

    out: dict = {}

    gender = (qs.get("gender") or [""])[0].upper()
    if gender in ("MEN", "WOMEN"):
        out["gender"] = gender

    sub_ids: list[int] = []
    for raw_key in ("subcategoryIds", "subcategoryId", "subcategory_ids"):
        for raw in qs.get(raw_key, []):
            for tok in raw.split(","):
                tok = tok.strip()
                if not tok:
                    continue
                try:
                    sub_ids.append(int(tok))
                except ValueError:
                    pass
    if sub_ids:
        # Dedup while preserving order — multiple subcategoryIds= entries
        # produce the same effect as a comma-separated list.
        seen: set[int] = set()
        out["subcategory_ids"] = [
            x for x in sub_ids if not (x in seen or seen.add(x))
        ]

    for ff_key, api_key in (("priceMin", "price_min"), ("priceMax", "price_max")):
        v = (qs.get(ff_key) or qs.get(api_key) or [""])[0]
        if v.strip().isdigit():
            out[api_key] = int(v.strip())

    brand = (qs.get("brand") or [""])[0].strip()
    if brand:
        out["brand"] = brand

    if not out.get("gender") and not out.get("subcategory_ids"):
        # Refuse a "give me literally everything" call — that's almost
        # always a copy-paste mistake (user pasted homepage URL).
        return None
    # API requires `gender` to be set; default to MEN if user didn't pick.
    out.setdefault("gender", "MEN")
    return out


def _parse_item(p: dict) -> SearchItem | None:
    pid = str(p.get("id") or "").strip()
    if not pid:
        return None
    if (p.get("status") or "").lower() != "selling":
        # Skip sold / paused listings — only push things the buyer can
        # actually buy.
        return None

    title_raw = (p.get("title") or "").strip() or "Без названия"
    brand = (p.get("brand") or "").strip() or None

    price_value = p.get("price")
    if isinstance(price_value, (int, float)):
        price_value = int(price_value)
        price_str = f"{price_value:,} ₩".replace(",", " ")
    else:
        price_value = None
        price_str = "Цена не указана"

    images = p.get("resizedSmallImages") or []
    image_url = images[0] if isinstance(images, list) and images else None

    item_url = f"{_PRODUCT_BASE}/{pid}"

    ts = _parse_iso8601(p.get("createdAt"))

    cond_raw = (p.get("condition") or "").strip().upper()
    condition = _CONDITION_LABELS.get(cond_raw) if cond_raw else None

    size = (p.get("size") or "").strip() or None

    return SearchItem(
        source=_HOST,
        external_id=pid,
        title=title_raw,
        price=price_str,
        price_value=price_value,
        url=item_url,
        image_url=image_url,
        location=None,
        description=None,
        seller_name=None,
        published_timestamp=ts,
        brand=brand,
        condition=condition,
        size=size,
        currency="KRW",
    )


def _parse_iso8601(s) -> int | None:
    """Parse "2026-04-29T19:12:42.000Z" into unix seconds. Returns None
    on failure so a single malformed timestamp doesn't drop the item."""
    if not isinstance(s, str) or not s:
        return None
    try:
        # Python 3.11+ accepts Z suffix; older needs +00:00 substitution.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return int(datetime.fromisoformat(s).astimezone(timezone.utc).timestamp())
    except Exception:
        return None
