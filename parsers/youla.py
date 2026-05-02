"""Youla (youla.ru) — Russian C2C marketplace, the country's #2 after Avito.

The web frontend uses Apollo with persisted queries against
`api-gw.youla.ru/graphql`. No auth needed for public catalog browsing —
the client identifies itself only with a generated `uid` and `app_id`.

Reverse-engineered from a live session on 2026-04-29:

  POST https://api-gw.youla.ru/graphql
  Headers:
    content-type: application/json
    accept: */*
    x-app-id: web/3
    x-uid: <16-hex>          (anonymous client id; we generate one)
    x-offset-utc: +03:00     (just for analytics, doesn't affect results)
  Body (catalogProductsBoard):
    {
      "operationName": "catalogProductsBoard",
      "variables": {
        "sort": "DATE_PUBLISHED_DESC",
        "attributes": [
          {"slug": "price", "value": null, "from": <int>, "to": <int>},
          {"slug": "categories", "value": [""], "from": null, "to": null}
        ],
        "datePublished": null,
        "location": {"latitude": null, "longitude": null,
                     "city": null, "distanceMax": null},
        "search": "<keyword>",
        "cursor": ""
      },
      "extensions": {"persistedQuery": {"version": 1,
                                        "sha256Hash": "<hash>"}}
    }

The persisted query hash is hard-coded — Apollo's APQ layer rejects
unknown hashes with PersistedQueryNotFound and does NOT auto-register
unauthenticated clients (we tested by replaying with a tweaked hash).
If Youla rotates the hash on a frontend release, this parser will
start returning None and the scheduler will mark the sub failed; bump
the hash from a fresh DevTools capture.

Trade-offs we accepted to ship a working v1:
- City is sent as null (search runs Russia-wide). The user URL's path
  segment ("/sankt-peterburg/...") is ignored. Adding a slug→city_id
  mapping is a future improvement; until then a Spb user sees results
  from Moscow + Spb + others.
- Items have no published_timestamp in the catalog response — we leave
  the field as None, scheduler renders "—" in the date line. Order in
  feed is newest-first thanks to DATE_PUBLISHED_DESC, so the dedup via
  external_id still works correctly.
- Promoted listings (`isPromoted=true`) are dropped to keep the feed
  consistent with «newest» semantics.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .base import SearchItem
from .common import MAX_JSON_BYTES, host_in_allowlist

logger = logging.getLogger(__name__)

_HOST = "youla"
_YOULA_HOSTS = frozenset({"youla.ru", "www.youla.ru", "m.youla.ru"})

_API_URL = "https://api-gw.youla.ru/graphql"
_BASE_URL = "https://youla.ru"
# Apollo persisted-query hash captured 2026-04-29. Update from a fresh
# browser DevTools session if Youla rotates it on a frontend release.
_PERSISTED_HASH = "6e7275a709ca5eb1df17abfb9d5d68212ad910dd711d55446ed6fa59557e2602"

_TIMEOUT = 25.0
_KEEP_TOP = 50


class YoulaSource:
    name = "youla"

    def matches(self, url: str) -> bool:
        return host_in_allowlist(url, _YOULA_HOSTS)

    async def fetch(
        self, url: str, proxy: str | None, max_retries: int = 3,
    ) -> list[SearchItem] | None:
        for attempt in range(max_retries):
            try:
                items = await _fetch_inner(url, proxy)
                if items is not None:
                    return items
            except Exception as e:
                logger.warning(
                    "[youla] attempt %d/%d failed: %s",
                    attempt + 1, max_retries, str(e)[:120],
                )
            await asyncio.sleep(2 + attempt * 2)
        return None


async def _fetch_inner(url: str, proxy: str | None) -> list[SearchItem] | None:
    keyword, price_from, price_to = _parse_user_url(url)
    if not keyword:
        logger.info("[youla] URL has no recognisable search keyword: %s", url[:120])
        return None

    attributes: list[dict] = [
        # An empty categories slug means "all categories" — Youla's web
        # UI sends this even when the user is on a specific category
        # page (it relies on `search` + the path-derived keyword to
        # narrow results).
        {"slug": "categories", "value": [""], "from": None, "to": None},
    ]
    if price_from is not None or price_to is not None:
        attributes.append({
            "slug": "price", "value": None,
            "from": price_from, "to": price_to,
        })

    body = {
        "operationName": "catalogProductsBoard",
        "variables": {
            "sort": "DATE_PUBLISHED_DESC",
            "attributes": attributes,
            "datePublished": None,
            "location": {
                "latitude": None, "longitude": None,
                "city": None, "distanceMax": None,
            },
            "search": keyword,
            "cursor": "",
        },
        "extensions": {
            "persistedQuery": {"version": 1, "sha256Hash": _PERSISTED_HASH},
        },
    }

    # Stable hex uid per process — Youla treats it as a session id.
    # Re-using it cycle-to-cycle keeps us from looking like a flood of
    # fresh anonymous visitors.
    uid = _client_uid()
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": _BASE_URL,
        "Referer": _BASE_URL + "/",
        "x-app-id": "web/3",
        "x-uid": uid,
        "appid": "web/3",
        "uid": uid,
        "x-offset-utc": "+03:00",
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
        logger.warning("[youla] network err: %s", str(e)[:120])
        return None

    if resp.status_code != 200:
        logger.warning("[youla] HTTP %d: %s", resp.status_code, resp.text[:200])
        return None

    # Body-size cap before JSON parse — defense against a hostile or
    # compromised upstream returning a 200 MB payload that OOMs the
    # 512 MB Railway container. The legitimate response is &lt;100 KB.
    body_bytes = resp.content
    if len(body_bytes) > MAX_JSON_BYTES:
        logger.warning(
            "[youla] response oversized: %d bytes — refusing to parse",
            len(body_bytes),
        )
        return None
    try:
        data = resp.json()
    except Exception as e:
        logger.warning("[youla] JSON decode err: %s", str(e)[:120])
        return None

    if data.get("errors"):
        msg = str(data["errors"])[:300]
        logger.warning("[youla] GraphQL errors: %s", msg)
        # PersistedQueryNotFound — Youla rotated the hash; surface this
        # loud so the operator notices in logs.
        if "PersistedQueryNotFound" in msg:
            logger.error(
                "[youla] persisted query hash invalidated — capture a "
                "fresh sha256Hash from DevTools and update _PERSISTED_HASH",
            )
        return None

    feed = ((data.get("data") or {}).get("feed") or {})
    raw_items = feed.get("items") or []
    items: list[SearchItem] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        if entry.get("__typename") != "ProductItem":
            # Skips LocationLabelPlacementItem, AdItem, etc.
            continue
        product = entry.get("product") or {}
        if product.get("isPromoted"):
            continue
        item = _parse_item(product)
        if item is not None:
            items.append(item)
        if len(items) >= _KEEP_TOP:
            break
    logger.info(
        "[youla] fetched %d items (search=%r price=%s..%s)",
        len(items), keyword[:60], price_from, price_to,
    )
    return items


# ---------------------------------------------------------------------------
# URL parsing — extract keyword and price range from a user-pasted URL
# ---------------------------------------------------------------------------

# Words that appear as path segments but aren't search keywords —
# these are city slugs the user picked from the geo dropdown. We strip
# them so "/sankt-peterburg/iphone-13" yields "iphone 13", not
# "sankt peterburg iphone 13".
_CITY_SLUGS: frozenset[str] = frozenset({
    "moskva", "sankt-peterburg", "novosibirsk", "ekaterinburg", "kazan",
    "nizhniy-novgorod", "samara", "chelyabinsk", "ufa", "rostov-na-donu",
    "krasnoyarsk", "perm", "voronezh", "volgograd", "krasnodar",
    "saratov", "tyumen", "tolyatti", "izhevsk", "barnaul",
    "irkutsk", "ulyanovsk", "habarovsk", "yaroslavl", "vladivostok",
    "tomsk", "orenburg", "kemerovo", "ryazan", "naberezhnye-chelny",
    "penza", "lipetsk", "tula", "kaliningrad",
})


def _parse_user_url(url: str) -> tuple[str | None, int | None, int | None]:
    """Pull (keyword, price_from, price_to) out of a Youla URL.

    Accepts both the raw form the user pastes (`/sankt-peterburg/iphone-13`)
    and the post-redirect form Youla rewrites it to
    (`/sankt-peterburg?q=iphone+13`). Returns (None, ...) when no keyword
    can be extracted — the caller should refuse to fetch in that case
    instead of silently fanning out to the entire site.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return None, None, None

    qs = parse_qs(parsed.query, keep_blank_values=False)
    keyword = (qs.get("q") or [""])[0].strip()
    if not keyword:
        # Derive from path: drop empty segments, drop city slugs, take
        # the first remaining segment, and turn dashes into spaces.
        segments = [
            s for s in (parsed.path or "").split("/")
            if s and s.lower() not in _CITY_SLUGS
        ]
        if segments:
            kw = unquote(segments[0]).replace("-", " ").strip()
            # Trim a trailing 24-hex object id if it's there (e.g. on
            # /<city>/<category>/<title>-<id>).
            kw = re.sub(r"\s+[a-f0-9]{24}$", "", kw)
            keyword = kw

    price_from = _try_int((qs.get("attributes[price][from]") or [None])[0])
    price_to   = _try_int((qs.get("attributes[price][to]")   or [None])[0])

    return (keyword or None), price_from, price_to


def _try_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Item mapping
# ---------------------------------------------------------------------------

def _parse_item(p: dict) -> SearchItem | None:
    pid = str(p.get("id") or "").strip()
    if not pid:
        return None

    title = (p.get("name") or "").strip() or "Без названия"

    # Price comes in kopecks under realPrice.price; realPriceText is
    # human-formatted but sometimes shows "35 ₽" for a 3500-kopeck price
    # (Youla quirk we don't fully understand) — so we trust the numeric
    # field and format ourselves.
    pp = (p.get("price") or {}).get("realPrice") or {}
    kopecks = pp.get("price")
    if isinstance(kopecks, (int, float)) and kopecks > 0:
        rubles = int(kopecks) // 100
        price_value = rubles
        price_str = f"{rubles:,} ₽".replace(",", " ")
    else:
        price_value = None
        # Fall back to the server-rendered string, which is at least
        # not misleading for unusual price types (e.g. salary postings).
        price_str = (
            (p.get("price") or {}).get("realPriceText") or "Цена не указана"
        )

    images = p.get("images") or []
    image_url = None
    if isinstance(images, list) and images:
        first = images[0] or {}
        image_url = first.get("url")

    rel_url = (p.get("url") or "").strip()
    if not rel_url:
        return None
    if rel_url.startswith("/"):
        item_url = _BASE_URL + rel_url
    else:
        # Absolute URLs from the API must point back at Youla. Anything
        # else (a misrouted ad, a redirector, or an upstream bug) gets
        # dropped — we don't want to hand the user a button that opens
        # an arbitrary off-site URL inside their Telegram client.
        try:
            host = (urlparse(rel_url).hostname or "").lower()
        except Exception:
            return None
        if host not in _YOULA_HOSTS:
            logger.warning("[youla] dropping item with off-site url host=%r", host)
            return None
        item_url = rel_url

    location = None
    loc = p.get("location") or {}
    city_name = (loc.get("cityName") or "").strip()
    if city_name:
        location = city_name
    elif p.get("distanceText"):
        location = str(p["distanceText"]).strip() or None

    return SearchItem(
        source=_HOST,
        external_id=pid,
        title=title,
        price=price_str,
        price_value=price_value,
        url=item_url,
        image_url=image_url,
        location=location,
        description=None,
        seller_name=None,
        # Youla's catalog response doesn't expose a publish timestamp —
        # the field would require a per-item GraphQL call we haven't
        # mapped. Leaving it None means the scheduler renders "—" for
        # the date line and skips the 2-day age filter (acceptable for
        # a fresh launch; revisit if dedup ever breaks).
        published_timestamp=None,
        currency="RUB",
    )


# ---------------------------------------------------------------------------
# Anonymous client uid — stable per process to look like a returning visitor
# ---------------------------------------------------------------------------

_uid_singleton: str | None = None


def _client_uid() -> str:
    global _uid_singleton
    if _uid_singleton is None:
        # 13 hex chars matches the format we observed in the wild
        # (e.g. "69f25e6ea5a0b" — 13 chars). Random-but-stable.
        _uid_singleton = secrets.token_hex(7)[:13]
    return _uid_singleton
