"""OLX (olx.pl / olx.ua / olx.ro / olx.com.br / ...) marketplace parser.

OLX does NOT ship a server-side hydration JSON blob on listing pages. Data
lives in the rendered DOM: `[data-cy="l-card"]` nodes, one per ad.

CRITICAL — organic vs promoted:
    Promoted ads (paid "TOP" placements) are mixed into the list and are
    NOT sorted by date. We detect them via href `search_reason=search|promoted`
    and SKIP them. Only organic items (`search_reason=search|organic`) are
    returned, so the monitor stays coherent when the user filters by "newest".
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

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

_HOST = "olx"
# olx.pl, olx.ua, olx.ro, olx.bg, olx.pt, olx.kz, olx.uz, olx.ba, olx.com.br, ...
_OLX_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?olx\.(?:com\.[a-z]{2}|[a-z]{2,3})/",
    re.IGNORECASE,
)

# "Today" literal per UI locale — covers the regions where OLX still operates.
# Values normalized to lowercase for matching.
_TODAY_WORDS = (
    "dzisiaj",      # PL
    "сьогодні",     # UA
    "сегодня",      # RU (kz, uz)
    "astăzi",       # RO
    "astazi",       # RO w/o diacritics
    "днес",         # BG
    "danas",        # BA/HR/RS
    "hoje",         # PT, BR
    "today",        # EN fallback
)

# "Yesterday" literal per locale — useful when OLX shows "Yesterday at HH:MM"
_YESTERDAY_WORDS = (
    "wczoraj",      # PL
    "вчора",        # UA
    "вчера",        # RU, BG
    "ieri",         # RO
    "juče",         # BA/HR/RS
    "ontem",        # PT, BR
    "yesterday",    # EN
)

# HH:MM extractor
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")

# Numeric absolute date: "19.04.2026"
_ABS_DATE_NUMERIC_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")

# Absolute date with month name: "19 kwietnia 2026", "19 kwietnia",
# "3 жовтня 2026", etc. Year is optional — defaults to current.
_ABS_DATE_WORD_RE = re.compile(r"(\d{1,2})\s+(\w+)(?:\s+(\d{4}))?", re.UNICODE)

# Month word → month number. Covers every OLX UI language we care about.
# Both nominative and genitive forms since some regions use "kwiecień"
# and others "kwietnia" ("19 of April" vs "April 19").
_MONTH_NAMES: dict[str, int] = {
    # Polish
    "styczeń": 1, "stycznia": 1, "sty": 1,
    "luty": 2, "lutego": 2, "lut": 2,
    "marzec": 3, "marca": 3, "mar": 3,
    "kwiecień": 4, "kwietnia": 4, "kwi": 4,
    "maj": 5, "maja": 5,
    "czerwiec": 6, "czerwca": 6, "cze": 6,
    "lipiec": 7, "lipca": 7, "lip": 7,
    "sierpień": 8, "sierpnia": 8, "sie": 8,
    "wrzesień": 9, "września": 9, "wrz": 9,
    "październik": 10, "października": 10, "paź": 10,
    "listopad": 11, "listopada": 11, "lis": 11,
    "grudzień": 12, "grudnia": 12, "gru": 12,
    # Ukrainian
    "січень": 1, "січня": 1,
    "лютий": 2, "лютого": 2,
    "березень": 3, "березня": 3,
    "квітень": 4, "квітня": 4,
    "травень": 5, "травня": 5,
    "червень": 6, "червня": 6,
    "липень": 7, "липня": 7,
    "серпень": 8, "серпня": 8,
    "вересень": 9, "вересня": 9,
    "жовтень": 10, "жовтня": 10,
    "листопад_ua": 11,  # same spelling as PL; handled by PL entry above
    "грудень": 12, "грудня": 12,
    # Russian (for olx.kz / olx.uz)
    "январь": 1, "января": 1, "янв": 1,
    "февраль": 2, "февраля": 2, "фев": 2,
    "март": 3, "марта": 3,
    "апрель": 4, "апреля": 4, "апр": 4,
    # "май" already above
    "июнь": 6, "июня": 6, "июн": 6,
    "июль": 7, "июля": 7, "июл": 7,
    "август": 8, "августа": 8, "авг": 8,
    "сентябрь": 9, "сентября": 9, "сен": 9,
    "октябрь": 10, "октября": 10, "окт": 10,
    "ноябрь": 11, "ноября": 11, "ноя": 11,
    "декабрь": 12, "декабря": 12, "дек": 12,
    # Romanian
    "ianuarie": 1, "februarie": 2, "martie": 3, "aprilie": 4,
    "iunie": 6, "iulie": 7,
    "septembrie": 9, "octombrie": 10, "noiembrie": 11, "decembrie": 12,
    # Portuguese (olx.pt, olx.com.br)
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
    # English (fallback)
    "january": 1, "february": 2, "march": 3, "april": 4,
    "june": 6, "july": 7,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Digits for price extraction
_DIGITS_RE = re.compile(r"[\d\s]+")

# Thumbnail size — OLX CDN templates: ";s=<w>x<h>;q=<q>"
_IMG_SIZE_RE = re.compile(r";s=\d+x\d+", re.IGNORECASE)

# Local TZ per country — OLX displays times in the user's regional timezone.
# Approximate mapping from domain TLD; good enough for "today" anchoring.
_TLD_TZ = {
    "pl": "Europe/Warsaw",
    "ua": "Europe/Kyiv",
    "ro": "Europe/Bucharest",
    "bg": "Europe/Sofia",
    "pt": "Europe/Lisbon",
    "kz": "Asia/Almaty",
    "uz": "Asia/Tashkent",
    "ba": "Europe/Sarajevo",
    "com.br": "America/Sao_Paulo",
}


class OlxSource:
    name = "olx"

    def matches(self, url: str) -> bool:
        return bool(_OLX_URL_RE.search(url or ""))

    async def fetch(
        self, url: str, proxy: str | None, max_retries: int = 3,
    ) -> list[SearchItem] | None:
        async with global_request_lock():
            try:
                return await _fetch_inner(url, proxy, max_retries)
            finally:
                import random
                cooldown = random.uniform(3.0, 7.0)
                logger.info("[olx] post-request cooldown %.1fs (lock held)", cooldown)
                await asyncio.sleep(cooldown)


# ---------------------------------------------------------------------------
# Fetch pipeline
# ---------------------------------------------------------------------------

async def _fetch_inner(url, proxy, max_retries):
    origin = _origin_for(url)
    warmup = (origin + "/",) if origin else ()
    for attempt in range(max_retries):
        items, blocked = await _fetch_html(url, proxy, warmup)
        if items is not None:
            logger.info("[olx] fetched %d items for %s", len(items), url[:80])
            return items
        if not blocked:
            return None
        logger.warning(
            "[olx] blocked (attempt %d/%d), rotating session%s",
            attempt + 1, max_retries, " + IP" if proxy else "",
        )
        invalidate_session(_HOST)
        # Rotating the mobile proxy is only useful when OLX was actually
        # reached through it; we normally run direct (proxy=None).
        if proxy:
            await rotate_ip()
        await asyncio.sleep(5)
    logger.error("[olx] all %d attempts blocked for %s", max_retries, url[:80])
    return None


async def _fetch_html(url, proxy, warmup):
    loop = asyncio.get_running_loop()
    resp_data = await loop.run_in_executor(
        None, lambda: _fetch_html_sync(url, proxy, warmup),
    )
    if resp_data is None:
        return None, False
    status, html, _headers = resp_data

    if status in (429, 403):
        logger.warning("[olx] BLOCKED %d for %s", status, url[:80])
        return None, True
    if status in (301, 302, 303, 307, 308):
        logger.warning("[olx] REDIRECT %d (block) for %s", status, url[:80])
        return None, True
    if status != 200:
        logger.debug("[olx] HTTP %d for %s", status, url[:80])
        return None, False
    logger.info("[olx] page loaded: %d, size=%d", status, len(html))

    items = _extract_items(html, url)
    if items is None:
        logger.warning("[olx] no l-card nodes in page (size=%d)", len(html))
        return None, False
    return items, False


def _fetch_html_sync(url, proxy, warmup):
    try:
        s = get_cloudscraper(_HOST, warmup_urls=list(warmup), proxy=proxy)
        proxies = proxies_dict(proxy)
        logger.info("[olx] REQUEST url=%r (len=%d)", url, len(url))
        resp = s.get(url, proxies=proxies, timeout=60, allow_redirects=True)
        logger.info(
            "[olx] response final_url=%r, status=%d",
            str(resp.url), resp.status_code,
        )
        return resp.status_code, resp.text, dict(resp.headers)
    except Exception as e:
        logger.debug("[olx] sync fetch error: %s", e)
        return None


def _origin_for(url: str) -> str:
    try:
        p = urlparse(url)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return ""


def _tld_for(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        if host.endswith(".com.br"):
            return "com.br"
        return host.rsplit(".", 1)[-1]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# DOM extraction
# ---------------------------------------------------------------------------

def _extract_items(html: str, url: str) -> list[SearchItem] | None:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("[olx] beautifulsoup4 not installed")
        return None

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select('[data-cy="l-card"]')
    if not cards:
        return None

    origin = _origin_for(url) or "https://www.olx.pl"
    tz = _tz_for_url(url)

    total_raw = _extract_total(soup)
    logger.info(
        "[olx] %d l-cards, total=%s, tz=%s",
        len(cards), total_raw, getattr(tz, "key", str(tz)),
    )

    items: list[SearchItem] = []
    skipped_promoted = 0
    for card in cards:
        try:
            it = _parse_card(card, origin, tz)
            if it is None:
                skipped_promoted += 1
                continue
            items.append(it)
        except Exception as e:
            logger.debug("[olx] parse card err: %s", e)

    logger.info(
        "[olx] parsed %d organic items (skipped %d promoted)",
        len(items), skipped_promoted,
    )

    if items:
        total_cnt = len(items)
        with_image = sum(1 for i in items if i.image_url)
        with_loc = sum(1 for i in items if i.location)
        with_desc = sum(1 for i in items if i.description)
        with_ts = sum(1 for i in items if i.published_timestamp)
        logger.info(
            "[olx] completeness: image=%d/%d, location=%d/%d, desc=%d/%d, date=%d/%d",
            with_image, total_cnt, with_loc, total_cnt,
            with_desc, total_cnt, with_ts, total_cnt,
        )
        sample = [i.url.replace(origin, "")[:50] for i in items[:3]]
        logger.info("[olx] sample item paths: %s", sample)
    return items


def _extract_total(soup) -> str | None:
    node = soup.select_one('[data-testid="total-count"]')
    if node is None:
        return None
    return (node.get_text() or "").strip() or None


def _parse_card(card, origin: str, tz) -> SearchItem | None:
    ext_id = (card.get("id") or "").strip()
    if not ext_id:
        return None

    link = card.select_one('a[href*="/oferta/"], a[href*="/d/oferta/"]')
    if link is None:
        return None
    href = link.get("href") or ""
    # Skip paid "TOP" placements — they break date-sorted monitoring.
    if _is_promoted(href):
        return None

    item_url = _absolutize(href, origin)

    title_node = card.select_one('[data-cy="ad-card-title"] h4, [data-cy="ad-card-title"] h6')
    if title_node is None:
        title_node = card.select_one('h4, h6')
    title = (title_node.get_text() or "").strip() if title_node else ""

    price_str, price_value = _extract_price(card)
    image_url = _extract_image_url(card)
    loc_text, ts = _extract_location_and_date(card, tz)

    return SearchItem(
        source="olx",
        external_id=ext_id,
        title=title,
        price=price_str,
        price_value=price_value,
        url=item_url,
        image_url=image_url,
        location=loc_text,
        description=None,   # description only on item page
        seller_name=None,   # seller only on item page
        published_timestamp=ts,
    )


def _is_promoted(href: str) -> bool:
    h = href.lower()
    # search_reason may be URL-encoded (%7C) or not (|)
    return (
        "search_reason=search%7cpromoted" in h
        or "search_reason=search|promoted" in h
    )


def _absolutize(href: str, origin: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        # Strip tracking query ?search_reason=... for a cleaner canonical URL
        return _strip_search_reason(href)
    if not href.startswith("/"):
        href = "/" + href
    return _strip_search_reason(origin + href)


def _strip_search_reason(url: str) -> str:
    try:
        p = urlparse(url)
        if not p.query:
            return url
        parts = [q for q in p.query.split("&") if not q.lower().startswith("search_reason=")]
        return urlunparse(p._replace(query="&".join(parts)))
    except Exception:
        return url


# ---------------------------------------------------------------------------
# Price / image / location / date
# ---------------------------------------------------------------------------

def _extract_price(card) -> tuple[str, int | None]:
    node = card.select_one('[data-testid="ad-price"]')
    if node is None:
        return "Цена не указана", None
    raw = (node.get_text(" ", strip=True) or "").strip()
    if not raw:
        return "Цена не указана", None
    # Pull out the leading numeric chunk for price_value
    m = _DIGITS_RE.search(raw)
    value = None
    if m:
        digits = re.sub(r"\D", "", m.group(0))
        if digits:
            try:
                value = int(digits)
            except ValueError:
                value = None
    # Cosmetic cleanup: insert a space between "złdo negocjacji" → "zł do negocjacji"
    raw = re.sub(r"([a-zA-Zł€$])(do|від|od|до)\b", r"\1 \2", raw)
    return raw, value


def _extract_image_url(card) -> str | None:
    img = card.select_one("img")
    if img is None:
        return None
    # OLX server-renders listing cards with lazy-loaded images — initial
    # `src` is usually a placeholder SVG; the real URL lives in `srcset`
    # or `data-src`. Try each spot before giving up.
    for attr in ("src", "data-src", "data-lazy-src", "data-original"):
        url = _clean_olx_image_url(img.get(attr) or "")
        if url:
            return url
    srcset = img.get("srcset") or ""
    if srcset:
        first = srcset.strip().split(",")[0].strip().split(" ")[0]
        url = _clean_olx_image_url(first)
        if url:
            return url
    return None


def _clean_olx_image_url(raw: str) -> str | None:
    if not raw:
        return None
    # Reject placeholder SVG / relative paths
    if raw.startswith("/") or raw.endswith(".svg"):
        return None
    if not raw.startswith("http"):
        return None
    # Upscale CDN thumbnail spec: ";s=216x152" → ";s=512x512"
    return _IMG_SIZE_RE.sub(";s=512x512", raw, count=1)


def _extract_location_and_date(card, tz) -> tuple[str | None, int | None]:
    node = card.select_one('[data-testid="location-date"]')
    if node is None:
        return None, None
    raw = (node.get_text() or "").strip()
    if not raw:
        return None, None
    # Format: "<City[, District]> - <Date phrase>"
    if " - " in raw:
        loc, date_part = raw.rsplit(" - ", 1)
        loc = loc.strip() or None
        ts = _parse_relative_date(date_part.strip(), tz)
    else:
        loc, ts = raw, None
    return loc, ts


def _parse_relative_date(phrase: str, tz) -> int | None:
    if not phrase:
        return None
    low = phrase.lower()
    time_m = _TIME_RE.search(low)
    hh, mm = (0, 0)
    if time_m:
        try:
            hh, mm = int(time_m.group(1)), int(time_m.group(2))
        except ValueError:
            hh, mm = 0, 0

    now = datetime.now(tz) if tz is not None else datetime.now(timezone.utc)

    if any(w in low for w in _TODAY_WORDS):
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        # If the parsed time is in the future relative to "now" (e.g. clock
        # skew / tz mismatch), accept as-is — scheduler's age filter is
        # tolerant.
        return int(target.timestamp())

    if any(w in low for w in _YESTERDAY_WORDS):
        target = (now - timedelta(days=1)).replace(
            hour=hh, minute=mm, second=0, microsecond=0,
        )
        return int(target.timestamp())

    # "19.04.2026" form
    m = _ABS_DATE_NUMERIC_RE.search(low)
    if m:
        try:
            day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            target = datetime(year, month, day, hh, mm, tzinfo=tz)
            return int(target.timestamp())
        except ValueError:
            pass

    # "19 kwietnia 2026" form (year optional)
    m = _ABS_DATE_WORD_RE.search(low)
    if m:
        day_s, month_word, year_s = m.group(1), m.group(2), m.group(3)
        month = _MONTH_NAMES.get(month_word)
        if month:
            try:
                day = int(day_s)
                year = int(year_s) if year_s else now.year
                target = datetime(year, month, day, hh, mm, tzinfo=tz)
                # If the resulting date is wildly in the future (seeing
                # "15 grudnia" in January → it's last year's ad, not next
                # year's), wrap back a year.
                if (target - now).days > 30:
                    target = target.replace(year=year - 1)
                return int(target.timestamp())
            except ValueError:
                pass

    return None


def _tz_for_url(url: str):
    tld = _tld_for(url)
    tz_name = _TLD_TZ.get(tld)
    if not tz_name:
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


# ---------------------------------------------------------------------------
# Image download with OLX referer
# ---------------------------------------------------------------------------

async def olx_download_image(url: str, proxy: str | None = None) -> bytes | None:
    # Referer domain hard-coded to .pl is OK — olxcdn accepts any olx.*
    return await download_image_bytes(
        url, host=_HOST, referer="https://www.olx.pl/", proxy=proxy,
    )
