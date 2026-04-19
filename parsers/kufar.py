"""Kufar (kufar.by) marketplace parser — Next.js __NEXT_DATA__ hydration JSON.

Kufar ships a server-rendered Redux store inside a <script id="__NEXT_DATA__">
JSON blob. The filter-applied listings live at
    props.initialState.listing.ads  (array of 40–50 organic items)
with filter markers `total`, `searchId`, `prevQuery` as siblings.
VIP items (`listing.vip`) are ignored — they're promoted, not filter-driven.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

import orjson

from .base import SearchItem
from .common import (
    download_image_bytes,
    get_cloudscraper,
    global_request_lock,
    invalidate_session,
    proxies_dict,
    rotate_ip,
)

logger = logging.getLogger(__name__)

_HOST = "kufar"
_WARMUP_URLS = ("https://www.kufar.by/",)
_KUFAR_URL_RE = re.compile(r"https?://(?:www\.|m\.)?kufar\.by/", re.IGNORECASE)

# Kufar image CDN. `path` in __NEXT_DATA__ is "adim1/<uuid>.jpg".
# list_thumbs_2x = the size the listings page itself renders.
_IMAGE_BASE = "https://rms.kufar.by/v1/list_thumbs_2x/"


class KufarSource:
    name = "kufar"

    def matches(self, url: str) -> bool:
        return bool(_KUFAR_URL_RE.search(url or ""))

    async def fetch(
        self, url: str, proxy: str | None, max_retries: int = 3,
    ) -> list[SearchItem] | None:
        async with global_request_lock():
            try:
                return await _fetch_inner(url, proxy, max_retries)
            finally:
                import random
                cooldown = random.uniform(3.0, 7.0)
                logger.info("[kufar] post-request cooldown %.1fs (lock held)", cooldown)
                await asyncio.sleep(cooldown)


# ---------------------------------------------------------------------------
# Fetch pipeline
# ---------------------------------------------------------------------------

async def _fetch_inner(
    url: str, proxy: str | None, max_retries: int,
) -> list[SearchItem] | None:
    for attempt in range(max_retries):
        items, blocked = await _fetch_hydration_json(url, proxy)
        if items is not None:
            logger.info("[kufar] fetched %d items for %s", len(items), url[:80])
            return items
        if not blocked:
            return None
        logger.warning(
            "[kufar] blocked (attempt %d/%d), rotating IP and retrying",
            attempt + 1, max_retries,
        )
        invalidate_session(_HOST)
        await rotate_ip()
        await asyncio.sleep(5)
    logger.error("[kufar] all %d attempts blocked for %s", max_retries, url[:80])
    return None


async def _fetch_hydration_json(url: str, proxy: str | None):
    loop = asyncio.get_running_loop()
    resp_data = await loop.run_in_executor(None, lambda: _fetch_html_sync(url, proxy))
    if resp_data is None:
        return None, False
    status, html, _headers = resp_data

    if status in (429, 403):
        logger.warning("[kufar] BLOCKED %d for %s", status, url[:80])
        return None, True
    if status in (301, 302, 303, 307, 308):
        logger.warning("[kufar] REDIRECT %d (block) for %s", status, url[:80])
        return None, True
    if status != 200:
        logger.debug("[kufar] HTTP %d for %s", status, url[:80])
        return None, False
    logger.info("[kufar] page loaded: %d, size=%d", status, len(html))

    items = _extract_items(html, url)
    if items is None:
        logger.warning("[kufar] no listing.ads in page (size=%d)", len(html))
        return None, False
    return items, False


def _fetch_html_sync(url: str, proxy: str | None):
    try:
        s = get_cloudscraper(_HOST, warmup_urls=list(_WARMUP_URLS), proxy=proxy)
        proxies = proxies_dict(proxy)
        logger.info("[kufar] REQUEST url=%r (len=%d)", url, len(url))
        resp = s.get(url, proxies=proxies, timeout=60, allow_redirects=False)
        logger.info("[kufar] response final_url=%r, status=%d",
                    str(resp.url), resp.status_code)
        return resp.status_code, resp.text, dict(resp.headers)
    except Exception as e:
        logger.debug("[kufar] sync fetch error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Catalog extraction from __NEXT_DATA__
# ---------------------------------------------------------------------------

def _extract_items(html: str, url: str) -> list[SearchItem] | None:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("[kufar] beautifulsoup4 not installed")
        return None

    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None:
        return None
    body = (script.text or "").strip()
    if not body:
        return None
    try:
        data = orjson.loads(body)
    except Exception as e:
        logger.warning("[kufar] __NEXT_DATA__ JSON decode err: %s", str(e)[:80])
        return None

    listing = (
        ((data.get("props") or {}).get("initialState") or {}).get("listing")
    ) or {}
    ads_raw = listing.get("ads")
    if not isinstance(ads_raw, list):
        logger.warning("[kufar] no listing.ads array (listing keys=%s)",
                       list(listing.keys())[:20])
        return None

    total = listing.get("total")
    search_id = listing.get("searchId") or ""
    prev_query = listing.get("prevQuery")
    has_markers = bool(total) and bool(search_id)
    logger.info(
        "[kufar] listing: %d ads, total=%s, searchId=%s, filter=%s",
        len(ads_raw), total, str(search_id)[:20], prev_query,
    )
    if not has_markers:
        logger.warning("[kufar] no filter markers — treating as unfiltered catalog")

    items: list[SearchItem] = []
    for raw in ads_raw:
        if not isinstance(raw, dict):
            continue
        if not (raw.get("ad_id") or raw.get("list_id")):
            continue
        try:
            items.append(_parse_item(raw))
        except Exception as e:
            logger.debug("[kufar] parse item err: %s", e)

    if items:
        total_cnt = len(items)
        with_image = sum(1 for i in items if i.image_url)
        with_loc = sum(1 for i in items if i.location)
        with_desc = sum(1 for i in items if i.description)
        with_ts = sum(1 for i in items if i.published_timestamp)
        logger.info(
            "[kufar] completeness: image=%d/%d, location=%d/%d, desc=%d/%d, date=%d/%d",
            with_image, total_cnt, with_loc, total_cnt,
            with_desc, total_cnt, with_ts, total_cnt,
        )
        sample = [i.url.replace("https://www.kufar.by", "")[:40] for i in items[:3]]
        logger.info("[kufar] sample item paths: %s", sample)
    return items


# ---------------------------------------------------------------------------
# Single-item parsing
# ---------------------------------------------------------------------------

def _parse_item(val: dict) -> SearchItem:
    ext_id = str(val.get("ad_id") or val.get("list_id") or "")
    title = val.get("subject") or ""

    price_str, price_value = _extract_price(val)
    item_url = val.get("ad_link") or f"https://www.kufar.by/item/{ext_id}"

    image_url = _extract_image_url(val)
    location = _extract_location(val)
    description = _extract_description(val)
    seller_name = _extract_seller(val)
    ts = _parse_list_time(val.get("list_time"))

    return SearchItem(
        source="kufar",
        external_id=ext_id,
        title=title,
        price=price_str,
        price_value=price_value,
        url=item_url,
        image_url=image_url,
        location=location,
        description=description,
        seller_name=seller_name,
        published_timestamp=ts,
    )


def _to_int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _pretty_num(v) -> str:
    n = _to_int(v)
    if n is None:
        return str(v) if v is not None else ""
    return f"{n:,}".replace(",", " ")


def _extract_price(val: dict) -> tuple[str, int | None]:
    byn = val.get("price_byn")
    usd = val.get("price_usd")
    byn_int = _to_int(byn)
    usd_int = _to_int(usd)

    if byn_int and byn_int > 0:
        if usd_int and usd_int > 0:
            return f"{_pretty_num(byn_int)} Br (~{_pretty_num(usd_int)} $)", byn_int
        return f"{_pretty_num(byn_int)} Br", byn_int
    if usd_int and usd_int > 0:
        return f"{_pretty_num(usd_int)} $", usd_int
    return "Цена не указана", None


def _extract_image_url(val: dict) -> str | None:
    imgs = val.get("images")
    if not isinstance(imgs, list) or not imgs:
        return None
    first = imgs[0]
    if not isinstance(first, dict):
        return None
    path = first.get("path")
    if not isinstance(path, str) or not path:
        return None
    return _IMAGE_BASE + path


def _extract_location(val: dict) -> str | None:
    params = val.get("ad_parameters")
    if not isinstance(params, list):
        return None
    region = None
    area = None
    for p in params:
        if not isinstance(p, dict):
            continue
        key = p.get("p")
        human = p.get("vl")
        if not isinstance(human, str) or not human.strip():
            continue
        human = human.strip()
        if key == "region":
            region = human
        elif key == "area":
            area = human
    parts = [x for x in (region, area) if x]
    if not parts:
        return None
    if len(parts) == 2 and parts[0] == parts[1]:
        return parts[0]
    return ", ".join(parts)


def _extract_description(val: dict) -> str | None:
    for k in ("body", "body_short"):
        v = val.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _extract_seller(val: dict) -> str | None:
    params = val.get("account_parameters")
    if not isinstance(params, list):
        return None
    for p in params:
        if isinstance(p, dict) and p.get("p") == "name":
            v = p.get("v")
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _parse_list_time(raw) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        n = int(raw)
        return n // 1000 if n > 1_000_000_000_000 else n
    if not isinstance(raw, str):
        return None
    # Observed format: "2026-04-19T17:54:08Z"
    try:
        dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        pass
    try:
        s = raw[:-1] if raw.endswith("Z") else raw
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Image download with Kufar referer (for Telegram photo upload)
# ---------------------------------------------------------------------------

async def kufar_download_image(url: str, proxy: str | None = None) -> bytes | None:
    return await download_image_bytes(
        url, host=_HOST, referer="https://www.kufar.by/", proxy=proxy,
    )
