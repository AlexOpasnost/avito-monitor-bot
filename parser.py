"""Avito parser — Playwright + stealth, single-browser-instance architecture.

Architecture:
  - bot.py calls init_browser() on startup -> ONE chromium instance for life of bot
  - Each monitoring check calls fetch_search_items(url) which:
      1. rotates the mobile proxy IP
      2. opens a NEW page in the existing browser
      3. applies stealth, navigates, scrolls, extracts window.__initialData__
      4. closes the page (browser stays up)
  - bot.py calls close_browser() on shutdown
"""
import asyncio
import logging
import random
import re
from dataclasses import dataclass
from urllib.parse import unquote

import httpx
import orjson
from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    async_playwright,
)

try:
    # playwright-stealth >= 1.0.6 ships an async stealth() helper
    from playwright_stealth import stealth_async  # type: ignore
except ImportError:  # pragma: no cover
    stealth_async = None

from config import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

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
# Browser singleton
# ---------------------------------------------------------------------------

_pw: Playwright | None = None
_browser: Browser | None = None
_browser_lock = asyncio.Lock()


async def init_browser() -> None:
    """Launch ONE chromium that stays up for the entire bot lifetime."""
    global _pw, _browser
    if _browser is not None:
        return
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
    )
    logger.info("[browser] chromium launched (singleton)")


async def close_browser() -> None:
    global _pw, _browser
    if _browser:
        try:
            await _browser.close()
        except Exception as e:
            logger.debug("[browser] close error: %s", e)
        _browser = None
    if _pw:
        try:
            await _pw.stop()
        except Exception as e:
            logger.debug("[browser] pw stop error: %s", e)
        _pw = None
    logger.info("[browser] chromium closed")


# ---------------------------------------------------------------------------
# Proxy rotation
# ---------------------------------------------------------------------------

async def rotate_ip() -> bool:
    """Hit the mobile-proxy rotation endpoint to get a fresh IP."""
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


def _proxy_dict_for_pw() -> dict | None:
    """Convert the configured proxy URL into Playwright's proxy dict shape."""
    if not config.proxy_list:
        return None
    proxy_url = config.proxy_list[0]
    # Expected format: scheme://[user:pass@]host:port
    m = re.match(r"^(https?|socks5)://(?:([^:@]+):([^@]+)@)?([^:/]+):(\d+)/?$", proxy_url)
    if not m:
        logger.warning("[proxy] cannot parse proxy URL: %s", proxy_url)
        return None
    scheme, user, password, host, port = m.groups()
    out: dict = {"server": f"{scheme}://{host}:{port}"}
    if user:
        out["username"] = user
    if password:
        out["password"] = password
    return out


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _ensure_sort_by_date(url: str) -> str:
    """Append &s=104 (sort by date desc) without re-encoding the URL."""
    if re.search(r"[?&]s=\d+", url):
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + "s=104"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def fetch_search_items(url: str, *_unused) -> list[AvitoItem] | None:
    """Open a fresh page in the singleton browser, scrape the results,
    close the page. Returns None on failure (caller logs and retries next cycle).

    Extra positional args are ignored — kept for backward compat with the
    old (url, proxy) signature."""
    if _browser is None:
        logger.error("[parser] browser not initialised — call init_browser() first")
        return None

    target_url = _ensure_sort_by_date(url)

    # Get a fresh mobile IP for this scrape
    await rotate_ip()
    await asyncio.sleep(0.5)

    proxy_cfg = _proxy_dict_for_pw()

    context: BrowserContext | None = None
    page = None
    try:
        async with _browser_lock:
            context = await _browser.new_context(
                user_agent=_USER_AGENT,
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                viewport={"width": 1366, "height": 900},
                proxy=proxy_cfg,
                extra_http_headers={
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                },
            )
        page = await context.new_page()

        if stealth_async:
            try:
                await stealth_async(page)
            except Exception as e:
                logger.debug("[parser] stealth_async failed: %s", e)

        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:
            logger.warning("[parser] goto failed for %s: %s", target_url[:80], e)
            return None

        # Wait for network to settle so __initialData__ is populated
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass  # networkidle is best-effort

        # Human-ish scroll
        try:
            await page.mouse.wheel(0, random.randint(300, 500))
            await asyncio.sleep(random.uniform(0.4, 1.0))
        except Exception:
            pass

        # IP-block detection — Avito serves a static page when the IP is banned
        try:
            title = await page.title()
        except Exception:
            title = ""
        if title and ("доступ ограничен" in title.lower() or "проблема с ip" in title.lower()):
            logger.warning("[parser] IP BLOCKED (title=%r) for %s", title, target_url[:80])
            return None

        items = await _extract_items_via_browser(page)
        if items is None:
            logger.warning("[parser] could not extract items for %s (title=%r)", target_url[:80], title[:60])
            return None
        logger.info("[parser] %d items for %s", len(items), target_url[:80])
        return items

    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass
        if context:
            try:
                await context.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Browser-side extraction
# ---------------------------------------------------------------------------

# JS that pulls every plausible hydration source AND falls back to DOM scrape
# inside the catalog-serp container only.
_EXTRACT_JS = r"""
() => {
    // 1) global hydration vars
    const globals = ['__initialData__', '__INITIAL_DATA__', '__preloadedState__',
                     '__PRELOADED_STATE__', '__mfe__', '__NEXT_DATA__'];
    for (const g of globals) {
        if (window[g] !== undefined && window[g] !== null) {
            return { source: 'global:' + g, data: window[g] };
        }
    }

    // 2) <script type="mime/invalid" data-mfe-state="true"> — Avito's modern shape
    const mfe = document.querySelectorAll('script[data-mfe-state="true"]');
    const mfeBlobs = [];
    for (const s of mfe) {
        const t = (s.textContent || '').trim();
        if (t && !t.includes('sandbox')) mfeBlobs.push(t);
    }
    if (mfeBlobs.length) return { source: 'mfe-state', data: mfeBlobs };

    // 3) DOM fallback — scrape inside catalog-serp only
    const root = document.querySelector('[data-marker="catalog-serp"]')
              || document.querySelector('[data-marker="catalog-list"]')
              || document.querySelector('.items-items')
              || null;
    if (!root) return { source: 'none', data: null };

    const items = [];
    const seen = new Set();
    for (const node of root.querySelectorAll('[data-item-id]')) {
        const id = node.getAttribute('data-item-id');
        if (!id || seen.has(id)) continue;
        seen.add(id);
        const link = node.querySelector('a[href*="' + id + '"]');
        const titleEl = node.querySelector('[itemprop="name"], h3, [data-marker="item-title"]');
        const priceEl = node.querySelector('[itemprop="price"], [data-marker="item-price"]');
        const imgEl = node.querySelector('img');
        const locEl = node.querySelector('[data-marker="item-address"], [class*="geo"]');
        items.push({
            id: id,
            title: titleEl ? titleEl.textContent.trim() : '',
            urlPath: link ? link.getAttribute('href').split('?')[0] : ('/' + id),
            priceStr: priceEl ? priceEl.textContent.trim() : '',
            priceValue: priceEl ? parseInt((priceEl.getAttribute('content') || priceEl.textContent).replace(/\D+/g, ''), 10) || null : null,
            imageUrl: imgEl ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src')) : null,
            location: locEl ? locEl.textContent.trim() : null,
        });
    }
    return { source: 'dom', data: items };
}
"""


async def _extract_items_via_browser(page) -> list[AvitoItem] | None:
    """Try multiple extraction sources via the live page. Returns a list of
    AvitoItems (possibly empty) or None on hard failure."""
    try:
        result = await page.evaluate(_EXTRACT_JS)
    except Exception as e:
        logger.debug("[parser] evaluate _EXTRACT_JS failed: %s", e)
        return None

    if not result or result.get("data") is None:
        return None

    source = result.get("source", "?")
    data = result["data"]
    logger.info("[parser] extraction source: %s", source)

    if source == "dom":
        # Already plain dicts shaped by our JS
        items: list[AvitoItem] = []
        for it in data:
            try:
                items.append(_item_from_dom_dict(it))
            except Exception as e:
                logger.debug("[parser] dom item err: %s", e)
        return items

    if source == "mfe-state":
        # List of JSON strings — try each
        for blob in data:
            try:
                parsed = orjson.loads(blob)
            except Exception:
                # Some scripts contain HTML-escaped JSON
                try:
                    import html as _html
                    parsed = orjson.loads(_html.unescape(blob))
                except Exception:
                    continue
            items = _parse_initial_data(parsed)
            if items:
                return items
        return []

    # Global hydration var — could be dict or string
    return _parse_initial_data(data)


def _item_from_dom_dict(d: dict) -> AvitoItem:
    item_id = str(d.get("id") or "")
    url_path = d.get("urlPath") or f"/{item_id}"
    if not url_path.startswith("http"):
        item_url = f"https://www.avito.ru{url_path}"
    else:
        item_url = url_path
    return AvitoItem(
        avito_id=item_id,
        title=d.get("title") or f"Объявление {item_id}",
        price=d.get("priceStr") or "Цена не указана",
        price_value=d.get("priceValue"),
        url=item_url,
        image_url=d.get("imageUrl"),
        location=d.get("location"),
        description=None,
        seller_name=None,
        published_timestamp=None,
    )


# ---------------------------------------------------------------------------
# __initialData__ parsing
# ---------------------------------------------------------------------------

def _parse_initial_data(raw) -> list[AvitoItem] | None:
    """`raw` may be a dict (already parsed) or a URL-encoded string."""
    data = None
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        try:
            decoded = unquote(raw)
            data = orjson.loads(decoded)
        except Exception:
            try:
                data = orjson.loads(raw)
            except Exception as e:
                logger.debug("[parser] cannot decode initial data: %s", e)
                return None
    else:
        return None

    items_raw = _find_catalog_items(data)
    if items_raw is None:
        return None

    items: list[AvitoItem] = []
    for raw_item in items_raw:
        if not isinstance(raw_item, dict):
            continue
        # Skip non-listing payloads (banners, recommendations) at the row level
        rtype = (raw_item.get("type") or "").strip()
        if rtype and rtype != "item":
            continue
        val = raw_item.get("value", raw_item) if "value" in raw_item else raw_item
        if not isinstance(val, dict):
            continue
        if not (val.get("id") or val.get("itemId")):
            continue
        try:
            items.append(_parse_item(val))
        except Exception as e:
            logger.debug("[parser] parse item err: %s", e)
    return items


# Catalog items live at one of these paths; everything else (recommendations,
# similar, vip, etc.) is intentionally NOT searched — Avito already filtered
# the catalog server-side based on the URL params.
def _find_catalog_items(data) -> list | None:
    if not isinstance(data, dict):
        return None
    candidates = [
        ["catalog", "items"],
        ["state", "data", "catalog", "items"],
        ["data", "catalog", "items"],
        ["initialData", "catalog", "items"],
        ["pageProps", "catalog", "items"],
        ["props", "catalog", "items"],
    ]
    for path in candidates:
        node = data
        ok = True
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                ok = False
                break
        if ok and isinstance(node, list):
            return node

    # Last resort: walk dict values looking for a "catalog" subdict with items
    for key, val in data.items():
        if key.lower() in ("recommendations", "similar", "vip", "promoted"):
            continue
        if isinstance(val, dict):
            sub = _find_catalog_items(val)
            if sub is not None:
                return sub
    return None


def _parse_item(val: dict) -> AvitoItem:
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

    image_url = _extract_image_url(val)

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
        if ts_int > 1_000_000_000_000:
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


# ---------------------------------------------------------------------------
# Image extraction
# ---------------------------------------------------------------------------

_IMAGE_SIZE_KEYS = (
    "864x648", "636x476", "540x405", "432x324", "318x238",
    "main", "big", "biggest", "default", "url",
)


def _extract_image_url(val: dict) -> str | None:
    for key in ("images", "imagesAlt", "photos", "gallery"):
        lst = val.get(key)
        if isinstance(lst, list) and lst:
            url = _image_from_dict(lst[0])
            if url:
                return url
    for key in ("image", "cover", "mainImage", "thumbnail"):
        obj = val.get(key)
        if isinstance(obj, dict):
            url = _image_from_dict(obj)
            if url:
                return url
        elif isinstance(obj, str) and obj.startswith("http"):
            return obj
    return None


def _image_from_dict(obj) -> str | None:
    if isinstance(obj, str) and obj.startswith("http"):
        return obj
    if not isinstance(obj, dict):
        return None
    for k in _IMAGE_SIZE_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    for nest_key in ("variants", "sizes", "urls"):
        nested = obj.get(nest_key)
        if isinstance(nested, dict):
            for k in _IMAGE_SIZE_KEYS:
                v = nested.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    return v
            for v in nested.values():
                if isinstance(v, str) and v.startswith("http"):
                    return v
    for v in obj.values():
        if isinstance(v, str) and v.startswith("http") and (
            ".jpg" in v or ".jpeg" in v or ".png" in v or ".webp" in v
        ):
            return v
    return None
