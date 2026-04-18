"""Avito parser — undetected-chromedriver (real Chrome, low-level patched).

Architecture:
  - ONE undetected_chromedriver.Chrome instance for the entire bot lifetime
  - Selenium is sync — async wrappers run all driver calls in a thread pool
    via asyncio.to_thread
  - All Avito interactions are serialized through a single asyncio.Lock so
    only one navigation happens at a time
  - Per-request: driver.get(url) -> wait for [data-marker="item"] ->
    human-like delays + scroll -> execute_script returns hydration payload
  - On block: rotate IP -> driver.get(url) again
"""
import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import unquote

import httpx
import orjson

# Optional Selenium imports — only needed at runtime in production.
# Unit tests don't need them.
# We use seleniumwire.undetected_chromedriver because authenticated proxies
# (user:pass@host:port) do NOT work with Chromium's --proxy-server flag —
# selenium-wire's mitmproxy-based tunnel handles auth transparently.
_uc_import_error: str | None = None
try:
    import seleniumwire.undetected_chromedriver as uc  # type: ignore
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
except Exception as _e:  # pragma: no cover
    uc = None  # type: ignore
    By = EC = WebDriverWait = None  # type: ignore
    _uc_import_error = f"{type(_e).__name__}: {_e}"

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
# Singleton driver
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_driver = None  # uc.Chrome | None — type hint omitted so unit tests can import without selenium
# All Selenium calls go through this lock — Selenium is single-threaded,
# and Avito should never see two simultaneous requests anyway.
_driver_lock = asyncio.Lock()


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


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def _build_driver_sync():
    """SYNC — must be called via asyncio.to_thread. Returns uc.Chrome."""
    if uc is None:
        raise RuntimeError(
            f"seleniumwire-undetected-chromedriver import failed: {_uc_import_error or 'not installed'}"
        )
    options = uc.ChromeOptions()
    options.add_argument("--lang=ru-RU,ru")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(f"--user-agent={_USER_AGENT}")
    options.add_argument("--window-size=1366,768")
    # selenium-wire intercepts HTTPS via a self-signed CA — Chrome must
    # trust it or every Avito page turns into a "Privacy error" screen.
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--ignore-ssl-errors=yes")
    options.set_capability("acceptInsecureCerts", True)

    # Proxy goes through selenium-wire (mitmproxy tunnel) so user:pass auth
    # is handled transparently — Chromium's --proxy-server does NOT support
    # credentials in the URL.
    sw_options: dict = {}
    if config.proxy_list:
        proxy = config.proxy_list[0]
        sw_options["proxy"] = {
            "http": proxy,
            "https": proxy,
            "no_proxy": "localhost,127.0.0.1",
        }
        # Redact creds in log
        redacted = re.sub(r"://[^@]+@", "://***@", proxy)
        logger.info("[parser] proxy bound via selenium-wire: %s", redacted)

    driver = uc.Chrome(
        options=options,
        headless=config.headless,
        no_sandbox=True,
        seleniumwire_options=sw_options,
    )
    driver.set_page_load_timeout(45)
    return driver


async def init_session() -> None:
    """Launch ONE undetected Chrome for the entire bot lifetime."""
    global _driver
    if _driver is not None:
        return
    _driver = await asyncio.to_thread(_build_driver_sync)
    logger.info(
        "[parser] undetected-chromedriver started (headless=%s, proxy=%s)",
        config.headless, "yes" if config.proxy_list else "no",
    )


async def close_session() -> None:
    global _driver
    if _driver is None:
        return
    drv = _driver
    _driver = None
    try:
        await asyncio.to_thread(drv.quit)
    except Exception as e:
        logger.debug("[parser] driver quit err: %s", e)
    logger.info("[parser] undetected-chromedriver stopped")


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------

async def rotate_ip() -> bool:
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
    if re.search(r"[?&]s=\d+", url):
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + "s=104"


# ---------------------------------------------------------------------------
# Public API — async wrapper around sync Selenium scrape
# ---------------------------------------------------------------------------

async def fetch_search_items(url: str, *_unused) -> list[AvitoItem] | None:
    """Navigate to URL and return parsed catalog items, or None on failure.
    Holds the global lock — only one Avito request at a time."""
    if _driver is None:
        logger.error("[parser] driver not initialised — call init_session() first")
        return None

    target_url = _ensure_sort_by_date(url)

    async with _driver_lock:
        try:
            payload = await asyncio.to_thread(_scrape_sync, target_url)
        except Exception as e:
            logger.warning("[parser] scrape error for %s: %s", target_url[:80], str(e)[:200])
            return None

        if payload is None:
            return None

        items = _items_from_payload(payload)
        if items is None:
            logger.warning("[parser] could not extract items from payload on %s", target_url[:80])
            return None
        logger.info("[parser] OK %d items for %s", len(items), target_url[:80])
        return items


# ---------------------------------------------------------------------------
# SYNC Selenium scrape — runs in a thread via asyncio.to_thread
# ---------------------------------------------------------------------------

def _scrape_sync(target_url: str):
    """Returns the hydration payload (dict/str) or None.
    May call into the proxy-rotation HTTP client through the event loop —
    we use asyncio.run_coroutine_threadsafe for that single side-effect."""
    # Random pre-navigation delay (human pace)
    time.sleep(random.uniform(2, 5))

    try:
        _driver.get(target_url)
    except Exception as e:
        logger.warning("[parser] driver.get failed: %s", str(e)[:200])
        return None

    # Wait for items to render
    if not _wait_for_items_sync(target_url):
        return None

    # Block check
    title = _safe_title_sync()
    if _looks_like_block(title):
        logger.warning(
            "[parser] BLOCKED (title=%r) for %s — rotating IP and retrying goto",
            title[:60], target_url[:80],
        )
        if config.proxy_rotate_url:
            _rotate_ip_sync()
            time.sleep(10)
        time.sleep(random.uniform(2, 5))
        try:
            _driver.get(target_url)
        except Exception as e:
            logger.warning("[parser] driver.get after rotation failed: %s", str(e)[:200])
            return None
        if not _wait_for_items_sync(target_url):
            return None
        title = _safe_title_sync()
        if _looks_like_block(title):
            logger.warning("[parser] still BLOCKED after rotation (title=%r)", title[:60])
            return None

    # Human-like post-load behavior
    time.sleep(random.uniform(3, 5))
    try:
        _driver.execute_script("window.scrollBy(0, arguments[0])", random.randint(300, 700))
    except Exception:
        pass
    time.sleep(random.uniform(1, 2))

    # Give Avito's SPA extra time to finish JS hydration after scroll
    time.sleep(5)

    # Diagnostic — what actually loaded?
    page_title = _safe_title_sync()
    try:
        page_len = len(_driver.page_source or "")
    except Exception:
        page_len = 0
    logger.info("[parser] loaded title=%r, page_source=%d chars", page_title[:80], page_len)

    # Try hydration sources one by one
    payload = None
    for var in ("__initialData__", "__preloadedState__", "__mfe__"):
        try:
            v = _driver.execute_script(f"return window.{var} || null;")
        except Exception as e:
            logger.debug("[parser] read window.%s failed: %s", var, str(e)[:120])
            continue
        if v:
            logger.info("[parser] hydration source: window.%s", var)
            payload = v
            break

    # data-mfe-state script blobs
    if payload is None:
        try:
            payload = _driver.execute_script(_MFE_STATE_JS)
        except Exception as e:
            logger.debug("[parser] mfe-state script extract failed: %s", str(e)[:120])
        if payload is not None:
            logger.info("[parser] hydration source: mfe-state script blob")

    # Last-resort: BeautifulSoup scrape of data-item-id inside catalog-serp
    if payload is None:
        logger.warning(
            "[parser] no hydration JSON — falling back to DOM scrape on %s",
            target_url[:80],
        )
        dom_items = _scrape_dom_fallback(_driver.page_source or "")
        if dom_items:
            # Return a synthetic payload that _items_from_payload will accept
            return {"state": {"data": {"catalog": {"items": dom_items}}}}
        logger.warning("[parser] DOM fallback also produced no items")
    return payload


# JS to pull the first data-mfe-state script blob that has state.data.catalog
_MFE_STATE_JS = r"""
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
"""


def _scrape_dom_fallback(html: str) -> list[dict]:
    """Pull items out of rendered HTML using BeautifulSoup. Only looks inside
    the main catalog container so recommendations/similar widgets are ignored."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("[parser] beautifulsoup4 not installed, DOM fallback unavailable")
        return []

    soup = BeautifulSoup(html, "html.parser")
    root = (
        soup.select_one('[data-marker="catalog-serp"]')
        or soup.select_one('[data-marker="catalog-list"]')
        or soup
    )

    items: list[dict] = []
    seen: set[str] = set()
    for node in root.select("[data-item-id]"):
        item_id = node.get("data-item-id") or ""
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)

        # URL
        link_el = node.select_one(f'a[href*="{item_id}"]') or node.select_one("a[href]")
        href = link_el.get("href", "") if link_el else ""
        url_path = href.split("?")[0] if href else f"/{item_id}"

        # Title
        title_el = (
            node.select_one('[itemprop="name"]')
            or node.select_one('[data-marker="item-title"]')
            or node.select_one("h3")
        )
        title = title_el.get_text(strip=True) if title_el else f"Объявление {item_id}"

        # Price
        price_el = (
            node.select_one('[itemprop="price"]')
            or node.select_one('[data-marker="item-price"]')
        )
        price_text = price_el.get_text(strip=True) if price_el else ""
        price_digits = re.sub(r"\D+", "", price_text)
        price_value = int(price_digits) if price_digits else None

        # Image
        img_el = node.select_one("img")
        img_src = ""
        if img_el:
            img_src = img_el.get("src") or img_el.get("data-src") or ""

        items.append({
            "id": item_id,
            "title": title,
            "urlPath": url_path,
            "priceDetailed": (
                {"value": price_value, "string": price_text} if price_value else (
                    {"string": price_text} if price_text else {}
                )
            ),
            "images": [{"url": img_src}] if img_src else [],
        })
    logger.info("[parser] DOM fallback extracted %d items", len(items))
    return items


def _wait_for_items_sync(target_url: str) -> bool:
    try:
        WebDriverWait(_driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '[data-marker="item"]'))
        )
        return True
    except Exception:
        title = _safe_title_sync()
        if _looks_like_block(title):
            return True  # caller handles block detection
        logger.warning(
            "[parser] wait_for_selector timed out on %s (title=%r)",
            target_url[:80], title[:60],
        )
        return True  # still try to extract


def _safe_title_sync() -> str:
    try:
        return _driver.title or ""
    except Exception:
        return ""


def _rotate_ip_sync() -> bool:
    """Sync IP rotation — uses httpx in sync mode, NOT the async one above.
    Called from within the threaded scrape so we can't await."""
    if not config.proxy_rotate_url:
        return False
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(config.proxy_rotate_url)
            return resp.status_code == 200
    except Exception as e:
        logger.warning("rotate_ip (sync) failed: %s", e)
        return False


def _looks_like_block(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(p in t for p in _BLOCK_PHRASES)


# JS to extract Avito hydration data
_EXTRACT_JS = r"""
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
"""


# ---------------------------------------------------------------------------
# Item extraction (strict main-catalog path only)
# ---------------------------------------------------------------------------

def _items_from_payload(payload) -> list[AvitoItem] | None:
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
