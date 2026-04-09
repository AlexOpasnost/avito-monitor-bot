"""Avito parser — primary: mobile API, fallback: HTML hydration JSON."""
import html as html_lib
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs

import httpx
import orjson

from config import config

logger = logging.getLogger(__name__)


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
# Public API
# ---------------------------------------------------------------------------

async def fetch_search_items(url: str, proxy: str | None) -> list[AvitoItem] | None:
    """Try mobile API first, fall back to HTML hydration JSON."""
    items = await _fetch_mobile_api(url, proxy)
    if items is not None:
        logger.debug("[api] fetched %d items for %s", len(items), url[:80])
        return items

    items = await _fetch_hydration_json(url, proxy)
    if items is not None:
        logger.debug("[html] fetched %d items for %s", len(items), url[:80])
    return items


async def rotate_ip() -> bool:
    """Call proxy rotation URL (if configured)."""
    if not config.proxy_rotate_url:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(config.proxy_rotate_url)
            return resp.status_code == 200
    except Exception as e:
        logger.warning("rotate_ip failed: %s", e)
        return False


async def check_proxy_ip() -> str | None:
    """Return external IP via the configured proxy, or None."""
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


# ---------------------------------------------------------------------------
# Mobile API
# ---------------------------------------------------------------------------

async def _fetch_mobile_api(url: str, proxy: str | None) -> list[AvitoItem] | None:
    """Fetch via the Avito mobile API. Returns None on failure (caller falls back)."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    params = {
        "key": config.avito_api_key,
        "locationId": "621540",  # Russia default
        "limit": "50",
        "display": "list",
        "sort": "date",
    }

    # Path-based hints: {location}/{category}/...
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    if path_parts:
        # We pass the full path so the API routes to the right catalog
        params["path"] = "/" + "/".join(path_parts)

    # Pass canonical filter params through
    if "f" in qs:
        params["f"] = qs["f"][0]
    if "q" in qs:
        params["query"] = qs["q"][0]
    if "pmin" in qs:
        params["priceMin"] = qs["pmin"][0]
    if "pmax" in qs:
        params["priceMax"] = qs["pmax"][0]
    if "s" in qs:
        params["sort"] = qs["s"][0]
    if "user" in qs:
        params["user"] = qs["user"][0]

    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=30, http2=False) as client:
            resp = await client.get(
                "https://m.avito.ru/api/11/items",
                params=params,
                headers={
                    "User-Agent": config.user_agent,
                    "Accept": "application/json",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                    "Referer": "https://m.avito.ru/",
                },
            )
            if resp.status_code != 200:
                logger.debug("[api] HTTP %d for %s", resp.status_code, url[:80])
                return None
            try:
                data = resp.json()
            except Exception:
                return None
            items_raw = (data.get("result") or {}).get("items") or []
            items: list[AvitoItem] = []
            for raw in items_raw:
                if raw.get("type") != "item":
                    continue
                val = raw.get("value") or {}
                try:
                    items.append(_parse_api_item(val))
                except Exception as e:
                    logger.debug("parse item err: %s", e)
            return items
    except Exception as e:
        logger.debug("[api] exception: %s", e)
        return None


# ---------------------------------------------------------------------------
# HTML fallback (hydration JSON)
# ---------------------------------------------------------------------------

_HYDRATION_RE = re.compile(
    r'<script[^>]*data-mfe-state="true"[^>]*>([^<]+)</script>',
    re.IGNORECASE,
)


async def _fetch_hydration_json(url: str, proxy: str | None) -> list[AvitoItem] | None:
    try:
        async with httpx.AsyncClient(
            proxy=proxy, timeout=60, follow_redirects=False,
        ) as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                },
            )
            if resp.status_code != 200:
                logger.debug("[html] HTTP %d for %s", resp.status_code, url[:80])
                return None
            html = resp.text
    except Exception as e:
        logger.debug("[html] exception: %s", e)
        return None

    match = _HYDRATION_RE.search(html)
    if not match:
        return None

    try:
        raw_json = html_lib.unescape(match.group(1))
        data = orjson.loads(raw_json)
    except Exception as e:
        logger.debug("[html] json parse err: %s", e)
        return None

    # Walk the state tree: common paths
    state = data.get("state") or data
    catalog = (state.get("data") or {}).get("catalog") or state.get("catalog") or {}
    items_raw = catalog.get("items") or []
    items: list[AvitoItem] = []
    for raw in items_raw:
        if isinstance(raw, dict) and raw.get("type") and raw.get("type") != "item":
            continue
        val = raw.get("value") if isinstance(raw, dict) and "value" in raw else raw
        if not isinstance(val, dict):
            continue
        try:
            items.append(_parse_api_item(val))
        except Exception as e:
            logger.debug("parse html item err: %s", e)
    return items if items else None


# ---------------------------------------------------------------------------
# Item parsing
# ---------------------------------------------------------------------------

def _parse_api_item(val: dict) -> AvitoItem:
    avito_id = str(val.get("id") or val.get("itemId") or "")
    title = val.get("title") or ""

    # Price
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

    # URL
    url_path = val.get("urlPath") or val.get("url") or ""
    if url_path and not url_path.startswith("http"):
        item_url = f"https://www.avito.ru{url_path}"
    else:
        item_url = url_path or "https://www.avito.ru"

    # Image
    image_url = None
    images = val.get("images") or []
    if images and isinstance(images[0], dict):
        img0 = images[0]
        image_url = (
            img0.get("864x648")
            or img0.get("636x476")
            or img0.get("432x324")
            or img0.get("url")
        )
        # Sometimes image is a dict of size->url under "variants"
        if not image_url and isinstance(img0.get("variants"), dict):
            variants = img0["variants"]
            image_url = (
                variants.get("864x648")
                or variants.get("636x476")
                or next(iter(variants.values()), None)
            )

    # Location
    location = None
    loc = val.get("location") or {}
    if isinstance(loc, dict):
        location = loc.get("name")

    # Description
    desc = val.get("description") or ""
    if isinstance(desc, dict):
        desc = desc.get("text") or ""
    if desc and len(desc) > 300:
        desc = desc[:300] + "..."

    # Seller
    seller = val.get("seller") or {}
    seller_name = seller.get("name") if isinstance(seller, dict) else None

    # Timestamp
    ts_raw = (
        val.get("sortTimeStamp")
        or val.get("time")
        or val.get("publishDate")
        or val.get("sortTime")
    )
    ts: int | None = None
    if isinstance(ts_raw, (int, float)):
        ts_int = int(ts_raw)
        if ts_int > 1_000_000_000_000:  # ms
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
