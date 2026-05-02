"""Grailed (grailed.com) — US/global resale fashion marketplace.

Search is powered by Algolia. The frontend ships a public read-only
API key in its JS bundle, so anyone with curl_cffi can hit the same
endpoint a browser would.

Endpoint:
  POST https://mnrwefss2q-dsn.algolia.net/1/indexes/*/queries

Reverse-engineered from a live recon on 2026-04-29:
  Application-Id : MNRWEFSS2Q
  API-Key        : c89dbaddf15fe70e1941a109bf7c2a3d  (search-only)
  Indexes:
    Listing_production                       — relevance default
    Listing_by_heat_recency_production       — newest first  ← we use this
    Listing_by_high_price_production
    Listing_by_low_price_production
    Listing_sold_by_high_price_production
    Listing_sold_by_low_price_production

Cloudflare gate: grailed.com sits behind Cloudflare's invisible
turnstile. Headless Chromium gets stuck on the challenge page; even
Playwright can't get through. curl_cffi with `impersonate="chrome120"`
DOES pass — it spoofs Chrome's TLS fingerprint at the bottom of the
stack, which is what Cloudflare actually checks at this strictness
level. Both the Cloudflare-fronted HTML and the Algolia API host pass
cleanly via this path, so we don't need a mobile proxy.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from .base import SearchItem
from .common import MAX_JSON_BYTES, host_in_allowlist

logger = logging.getLogger(__name__)

_HOST = "grailed"
_GRAILED_HOSTS = frozenset({
    "grailed.com", "www.grailed.com", "m.grailed.com",
})

_ALGOLIA_HOST = "mnrwefss2q-dsn.algolia.net"
_ALGOLIA_APP_ID = "MNRWEFSS2Q"
_ALGOLIA_API_KEY = "c89dbaddf15fe70e1941a109bf7c2a3d"
_INDEX = "Listing_by_heat_recency_production"

_TIMEOUT = 25.0
_HITS_PER_PAGE = 40
_KEEP_TOP = 50

# Item URL skeleton — Grailed redirects /listings/<id> onto the
# canonical /listings/<id>-<slug> on every fetch, so we don't need to
# care about the slug when posting a notification link.
_ITEM_URL_TEMPLATE = "https://www.grailed.com/listings/{}"

# Map Algolia condition enums to short Russian labels. Translation
# into the user's language happens in scheduler._localise_item.
_CONDITION_LABELS: dict[str, str] = {
    "is_new":          "Новое с биркой",
    "is_gently_used":  "Б/у, отличное",
    "is_used":         "Б/у",
    "is_worn":         "Поношенное",
    "is_distressed":   "Сильно ношенное",
}

# Top-level URL paths we know how to handle. Personal feeds (/feed/<token>)
# and saved searches require auth — refuse them up front so users get
# a clear "не поддерживается" message instead of empty results forever.
_SUPPORTED_PATH_PREFIXES = ("/shop", "/categories", "/designers")


class GrailedSource:
    name = "grailed"

    def matches(self, url: str) -> bool:
        return host_in_allowlist(url, _GRAILED_HOSTS)

    async def fetch(
        self, url: str, proxy: str | None, max_retries: int = 3,
    ) -> list[SearchItem] | None:
        for attempt in range(max_retries):
            try:
                items = await asyncio.get_running_loop().run_in_executor(
                    None, _fetch_sync, url, proxy,
                )
                if items is not None:
                    return items
            except Exception as e:
                logger.warning(
                    "[grailed] attempt %d/%d failed: %s",
                    attempt + 1, max_retries, str(e)[:120],
                )
            await asyncio.sleep(2 + attempt * 2)
        return None


def _fetch_sync(url: str, proxy: str | None) -> list[SearchItem] | None:
    """Sync core — wrapped in run_in_executor by the async caller.
    curl_cffi is sync-only, and its TLS impersonation is what gets us
    past Cloudflare; using httpx here would put us back on the
    challenge page."""
    from curl_cffi import requests

    params = _algolia_params_from_url(url)
    if params is None:
        return None

    body = {
        "requests": [
            {"indexName": _INDEX, "params": params}
        ]
    }
    headers = {
        "X-Algolia-Application-Id": _ALGOLIA_APP_ID,
        "X-Algolia-API-Key": _ALGOLIA_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://www.grailed.com",
        "Referer": "https://www.grailed.com/",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None

    try:
        resp = requests.post(
            f"https://{_ALGOLIA_HOST}/1/indexes/*/queries",
            json=body,
            headers=headers,
            proxies=proxies,
            timeout=_TIMEOUT,
            impersonate="chrome120",
            allow_redirects=False,
        )
    except Exception as e:
        logger.warning("[grailed] network err: %s", str(e)[:120])
        return None

    if resp.status_code != 200:
        logger.warning(
            "[grailed] HTTP %d body=%s",
            resp.status_code, resp.text[:300],
        )
        return None
    # Body-size cap before JSON parse — see common.MAX_JSON_BYTES rationale.
    # curl_cffi exposes raw bytes via .content the same way httpx does.
    body_bytes = resp.content
    if len(body_bytes) > MAX_JSON_BYTES:
        logger.warning(
            "[grailed] response oversized: %d bytes",
            len(body_bytes),
        )
        return None
    try:
        data = resp.json()
    except Exception as e:
        logger.warning("[grailed] JSON decode err: %s", str(e)[:120])
        return None

    results = (data.get("results") or [{}])[0]
    hits = results.get("hits") or []
    items: list[SearchItem] = []
    for h in hits[:_KEEP_TOP]:
        item = _parse_hit(h)
        if item is not None:
            items.append(item)
    logger.info(
        "[grailed] fetched %d items (params=%s)", len(items), params[:120],
    )
    return items


# ---------------------------------------------------------------------------
# URL → Algolia params translation
# ---------------------------------------------------------------------------

def _algolia_params_from_url(url: str) -> str | None:
    """Build the `params` URL-encoded string Algolia expects.

    Supported user URL shapes:
      /shop?query=stone+island&hitsPerPage=40
      /categories/menswear/tops               (no free text)
      /categories/womenswear/dresses
      /designers/stone-island

    For personal feeds (/feed/…) or anything we don't recognise we
    return None so the caller refuses the subscription instead of
    silently fanning out to the entire site.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    path = (parsed.path or "/").rstrip("/") or "/"

    qs = parse_qs(parsed.query, keep_blank_values=False)
    query_text = (qs.get("query") or [""])[0].strip()

    facet_filters: list[list[str]] = []
    optional_facet_filters: list[str] = []

    if path.startswith("/categories"):
        # /categories/{department}/{category} — both segments map
        # one-to-one onto Algolia facets. Department only when present.
        segs = [s for s in path.split("/") if s and s != "categories"]
        if len(segs) >= 1:
            facet_filters.append([f"department:{segs[0]}"])
        if len(segs) >= 2:
            facet_filters.append([f"category:{segs[1]}"])
    elif path.startswith("/designers"):
        # /designers/{slug} — Grailed slug is hyphen-separated lowercase
        # designer name. Algolia's `designers.name` facet uses the
        # original "Stone Island" form, so dash→space + don't lowercase
        # the actual filter value (Algolia is case-insensitive on facet
        # values). We use strict facetFilters (not optionalFacetFilters)
        # so the result is a hard filter; otherwise designer becomes a
        # relevance boost and unrelated brands leak in.
        segs = [s for s in path.split("/") if s and s != "designers"]
        if segs:
            designer = segs[0].replace("-", " ")
            facet_filters.append([f"designers.name:{designer}"])
    elif path.startswith("/shop"):
        # query in querystring; nothing to add to facets
        pass
    elif path == "/" and query_text:
        # Bare homepage with ?query=… is also a search
        pass
    else:
        # Personal feeds, login pages, etc. — refuse.
        return None

    # No need to filter sold/dropped/deleted — the
    # Listing_by_heat_recency_production index excludes them at index
    # time (verified empirically). A facet filter on those booleans
    # actually returns zero hits because Algolia indexes them as
    # numeric 0/1, not strings.

    parts = [
        f"query={_url_encode(query_text)}",
        f"hitsPerPage={_HITS_PER_PAGE}",
        "page=0",
    ]
    if facet_filters:
        parts.append(f"facetFilters={_json_url_encode(facet_filters)}")
    if optional_facet_filters:
        parts.append(f"optionalFacetFilters={_json_url_encode(optional_facet_filters)}")

    # If neither query nor any facet was extracted, refuse — better to
    # tell the user "не поддерживается" than to push every recent
    # listing on Grailed at them every minute.
    if not query_text and not facet_filters and not optional_facet_filters:
        return None
    return "&".join(parts)


def _url_encode(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


def _json_url_encode(value) -> str:
    """Algolia wants `facetFilters` as a URL-encoded JSON array."""
    import json
    from urllib.parse import quote
    return quote(json.dumps(value, ensure_ascii=False), safe="")


# ---------------------------------------------------------------------------
# Hit → SearchItem
# ---------------------------------------------------------------------------

def _parse_hit(h: dict) -> SearchItem | None:
    if not isinstance(h, dict):
        return None
    if h.get("sold") or h.get("dropped") or h.get("deleted"):
        return None

    obj_id = h.get("objectID") or h.get("id")
    if obj_id is None:
        return None
    pid = str(obj_id)

    title = (h.get("title") or "").strip() or "Без названия"

    # Price is a plain integer in USD (we observed `price=888` for an
    # $888 listing — not cents). `price_i` is the same value, kept for
    # historical Algolia typing.
    price_value = h.get("price")
    if not isinstance(price_value, (int, float)) or price_value <= 0:
        price_value = h.get("price_i")
    if isinstance(price_value, (int, float)) and price_value > 0:
        price_value = int(price_value)
        price_str = f"${price_value:,}".replace(",", " ")
    else:
        price_value = None
        price_str = "Цена не указана"

    cover = h.get("cover_photo") or {}
    image_url = cover.get("url") or cover.get("image_url")

    item_url = _ITEM_URL_TEMPLATE.format(pid)

    # Designer name as a separate field — scheduler renders it as a
    # standalone line in the notification card.
    brand = None
    designer_names = (h.get("designer_names") or "").strip()
    if designer_names:
        brand = designer_names
    elif isinstance(h.get("designers"), list) and h["designers"]:
        first = h["designers"][0]
        if isinstance(first, dict) and first.get("name"):
            brand = str(first["name"]).strip()

    cond_raw = (h.get("condition") or "").strip().lower()
    condition = _CONDITION_LABELS.get(cond_raw) if cond_raw else None

    size = (h.get("size") or "").strip() or None

    location = (h.get("location") or "").strip() or None

    # Prefer created_at_i (epoch). Fall back to parsing the ISO string
    # if Algolia ever ships only the human form.
    ts = h.get("created_at_i")
    if not isinstance(ts, (int, float)) or ts <= 0:
        ts = _parse_iso8601(h.get("created_at"))
    else:
        ts = int(ts)

    seller_name = None
    user = h.get("user") or {}
    if isinstance(user, dict):
        seller_name = (user.get("username") or "").strip() or None

    return SearchItem(
        source=_HOST,
        external_id=pid,
        title=title,
        price=price_str,
        price_value=price_value,
        url=item_url,
        image_url=image_url,
        location=location,
        # Algolia's compact hit doesn't carry a description. The full
        # listing page does, but enriching every new item costs a CF-
        # gated round-trip per item, which we'll add later only if
        # users complain.
        description=None,
        seller_name=seller_name,
        published_timestamp=ts,
        brand=brand,
        condition=condition,
        size=size,
        currency="USD",
    )


def _parse_iso8601(s) -> int | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return int(datetime.fromisoformat(s).astimezone(timezone.utc).timestamp())
    except Exception:
        return None
