"""Avito parser — Playwright (real Chromium) singleton + per-subscription pages.

Architecture (per spec):
  - ONE Playwright Chromium for the entire bot lifetime
  - ONE BrowserContext (locale ru-RU, tz Europe/Moscow, viewport 1366x768,
    real Chrome Windows User-Agent, mobile proxy bound at context level)
  - Per-subscription Page registered by URL — kept open across cycles
  - Each cycle: page.reload() → wait networkidle → extract → parse
  - On block (title contains "Доступ ограничен"): rotate IP, page.reload()
  - Browser is NEVER recreated on IP rotation — same context, same page
"""
import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import unquote

import httpx
import orjson
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

try:
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
# Singleton browser + per-subscription pages
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_pw: Playwright | None = None
_browser: Browser | None = None
_context: BrowserContext | None = None
_pages: dict[str, Page] = {}  # key (URL) -> Page
_pages_lock = asyncio.Lock()
# Serialize ALL Avito interactions across the whole bot — only one
# scrape (reload + evaluate) at a time even with many subs.
_avito_lock = asyncio.Lock()


# Strict catalog paths inside the hydration payload
_MAIN_CATALOG_PATH = ("state", "data", "catalog", "items")
_FALLBACK_CATALOG_PATHS = (
    ("data", "catalog", "items"),
    ("initialData", "catalog", "items"),
    ("pageProps", "catalog", "items"),
)

_BLOCK_PHRASES = (
    "доступ ограничен",
    "проблема с ip",
    "подозрительная активность",
    "слишком много запросов",
    "robot check",
    "captcha",
)


def _proxy_dict_for_pw() -> dict | None:
    """Convert proxy URL into Playwright's proxy dict shape:
       {"server": "scheme://host:port", "username": ..., "password": ...}"""
    if not config.proxy_list:
        return None
    p = config.proxy_list[0]
    m = re.match(r"^(https?|socks5)://(?:([^:@]+):([^@]+)@)?([^:/]+):(\d+)/?$", p)
    if not m:
        logger.warning("[proxy] cannot parse: %s", p)
        return None
    scheme, user, password, host, port = m.groups()
    out: dict = {"server": f"{scheme}://{host}:{port}"}
    if user:
        out["username"] = user
    if password:
        out["password"] = password
    return out


# ---------------------------------------------------------------------------
# Lifecycle (called from bot.py on startup/shutdown)
# ---------------------------------------------------------------------------

async def init_session() -> None:
    """Launch ONE Chromium + ONE BrowserContext for the entire bot lifetime."""
    global _pw, _browser, _context
    if _browser is not None:
        return

    proxy_cfg = _proxy_dict_for_pw()

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
        ],
    )
    _context = await _browser.new_context(
        user_agent=_USER_AGENT,
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        viewport={"width": 1366, "height": 768},
        proxy=proxy_cfg,
        extra_http_headers={
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
    )
    logger.info(
        "[parser] Playwright Chromium + context launched (proxy=%s, locale=ru-RU, tz=Europe/Moscow)",
        "yes" if proxy_cfg else "no",
    )


async def close_session() -> None:
    global _pw, _browser, _context, _pages
    for url, page in list(_pages.items()):
        try:
            await page.close()
        except Exception:
            pass
    _pages.clear()
    if _context:
        try:
            await _context.close()
        except Exception:
            pass
        _context = None
    if _browser:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None
    if _pw:
        try:
            await _pw.stop()
        except Exception:
            pass
        _pw = None
    logger.info("[parser] Playwright closed")


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------

async def rotate_ip() -> bool:
    """Hit the mobile-proxy rotation endpoint to get a fresh IP. The browser
    keeps the same context+proxy URL — only the underlying mobile IP changes,
    so a page.reload() will use it automatically."""
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
# URL helpers
# ---------------------------------------------------------------------------

def _ensure_sort_by_date(url: str) -> str:
    """Append &s=104 (sort by date desc) without re-encoding the URL."""
    if re.search(r"[?&]s=\d+", url):
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + "s=104"


# ---------------------------------------------------------------------------
# Page registry — one page per subscription URL, kept open across cycles
# ---------------------------------------------------------------------------

async def _get_or_create_page(target_url: str) -> Page:
    """Get the existing page for this URL or create+navigate a new one.
    Caller MUST hold _avito_lock."""
    if target_url in _pages:
        page = _pages[target_url]
        if not page.is_closed():
            return page
        # Stale entry — page got closed somehow
        del _pages[target_url]

    page = await _context.new_page()
    if stealth_async:
        try:
            await stealth_async(page)
        except Exception as e:
            logger.debug("[parser] stealth_async failed: %s", e)
    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=45_000)
    except Exception as e:
        logger.warning("[parser] initial goto failed for %s: %s", target_url[:80], e)
        try:
            await page.close()
        except Exception:
            pass
        raise
    _pages[target_url] = page
    logger.info("[parser] page created for %s", target_url[:80])
    return page


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def fetch_search_items(url: str, *_unused) -> list[AvitoItem] | None:
    """Reload the per-subscription page, extract catalog items.

    Holds the GLOBAL avito-lock for the entire reload+extract so two
    subs never reload simultaneously. On block: rotate IP and reload
    once more; if still blocked, return None and let the scheduler
    skip the cycle."""
    if _browser is None:
        logger.error("[parser] browser not initialised — call init_session() first")
        return None

    target_url = _ensure_sort_by_date(url)

    async with _avito_lock:
        try:
            page = await _get_or_create_page(target_url)
        except Exception as e:
            logger.warning("[parser] could not create page: %s", str(e)[:200])
            return None

        return await _scrape_page(page, target_url)


async def _scrape_page(page: Page, target_url: str) -> list[AvitoItem] | None:
    try:
        await page.reload(wait_until="domcontentloaded", timeout=45_000)
    except Exception as e:
        logger.warning("[parser] reload failed for %s: %s", target_url[:80], str(e)[:200])
        return None

    try:
        await page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass  # networkidle is best-effort

    title = await _safe_title(page)
    if _looks_like_block(title):
        logger.warning(
            "[parser] BLOCKED (title=%r) for %s — rotating IP and reloading",
            title[:60], target_url[:80],
        )
        await rotate_ip()
        await asyncio.sleep(2)
        try:
            await page.reload(wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:
            logger.warning("[parser] reload after rotation failed: %s", str(e)[:200])
            return None
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass
        title = await _safe_title(page)
        if _looks_like_block(title):
            logger.warning("[parser] still BLOCKED after IP rotation+reload (title=%r)", title[:60])
            return None

    # Extract hydration payload via the page itself
    try:
        payload = await page.evaluate(_EXTRACT_JS)
    except Exception as e:
        logger.warning("[parser] page.evaluate failed: %s", str(e)[:200])
        return None

    if payload is None:
        logger.warning("[parser] no hydration data on %s", target_url[:80])
        return None

    items = _items_from_payload(payload)
    if items is None:
        logger.warning("[parser] could not extract items from payload on %s", target_url[:80])
        return None
    logger.info("[parser] OK %d items for %s", len(items), target_url[:80])
    return items


async def _safe_title(page: Page) -> str:
    try:
        return await page.title()
    except Exception:
        return ""


def _looks_like_block(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(p in t for p in _BLOCK_PHRASES)


# JS executed inside the page. Tries window globals first, then walks
# data-mfe-state script tags returning the FIRST blob whose
# state.data.catalog exists. We do NOT extract items here — Python-side
# code applies the strict path lookup and item parsing.
_EXTRACT_JS = r"""
() => {
  for (const g of ['__initialData__', '__preloadedState__', '__mfe__']) {
    const v = window[g];
    if (v !== undefined && v !== null) return v;
  }
  const scripts = document.querySelectorAll('script[data-mfe-state="true"]');
  for (const s of scripts) {
    const txt = (s.textContent || '').trim();
    if (!txt || txt.includes('sandbox')) continue;
    try {
      const data = JSON.parse(txt);
      if (data && data.state && data.state.data && data.state.data.catalog) {
        return data;
      }
    } catch (e) {}
  }
  return null;
}
"""


# ---------------------------------------------------------------------------
# Item extraction (strict main-catalog path only)
# ---------------------------------------------------------------------------

def _items_from_payload(payload) -> list[AvitoItem] | None:
    """Walk the hydration payload to the main catalog and parse items.
    NEVER recurses into recommendations/similar/viewedItems blocks."""
    data = payload
    if isinstance(data, str):
        try:
            data = orjson.loads(unquote(data))
        except Exception:
            try:
                data = orjson.loads(data)
            except Exception:
                return None
    if not isinstance(data, dict):
        return None

    items_raw = _walk_path(data, _MAIN_CATALOG_PATH)
    used_path = ".".join(_MAIN_CATALOG_PATH)
    if items_raw is None:
        for path in _FALLBACK_CATALOG_PATHS:
            items_raw = _walk_path(data, path)
            if items_raw is not None:
                used_path = ".".join(path)
                break
    if items_raw is None:
        logger.warning("[parser] no main catalog in payload (top keys: %s)", list(data.keys())[:8])
        return None

    items = _items_from_raw_list(items_raw)
    if items:
        logger.info("[parser] extracted from %s — %d listings (raw=%d)", used_path, len(items), len(items_raw))
    return items


def _walk_path(data, path: tuple[str, ...]):
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node if isinstance(node, list) else None


def _items_from_raw_list(items_raw) -> list[AvitoItem]:
    """Convert the raw catalog.items list into AvitoItems. Skips wrappers
    that are not 'item' type (banners/snippets if Avito ever inlines them)."""
    items: list[AvitoItem] = []
    for raw_item in items_raw:
        if not isinstance(raw_item, dict):
            continue
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


def _parse_item(val: dict) -> AvitoItem:
    avito_id = str(val.get("id") or val.get("itemId") or "")
    title = val.get("title") or ""

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

    url_path = val.get("urlPath") or val.get("url") or ""
    if url_path and not url_path.startswith("http"):
        item_url = f"https://www.avito.ru{url_path}"
    else:
        item_url = url_path or "https://www.avito.ru"

    image_url = _extract_image_url(val)

    location = None
    loc = val.get("location") or {}
    if isinstance(loc, dict):
        location = loc.get("name")

    desc = val.get("description") or ""
    if isinstance(desc, dict):
        desc = desc.get("text") or ""
    if desc and len(desc) > 300:
        desc = desc[:300] + "..."

    seller = val.get("seller") or {}
    seller_name = seller.get("name") if isinstance(seller, dict) else None

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
    "864x864", "636x636", "472x472", "432x432",
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
