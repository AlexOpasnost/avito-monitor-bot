"""Per-subscription async scheduler."""
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from aiogram.types import BufferedInputFile

from config import config
from database import db
from parser import (
    SearchItem,
    detect_source,
    download_image_bytes,
    fetch_search_items,
)
from bot_i18n import default_tz_for_lang, timezone_short
from parsers import source_display_name
from parsers.common import proxy_for_source
from parsers.currency import format_with_estimate

# Back-compat alias used in this module
AvitoItem = SearchItem

logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))
MAX_ITEMS_PER_CYCLE = 10
MAX_AGE_SECONDS = 2 * 24 * 3600  # 2 days


async def run_scheduler(bot: Bot, stop_event: asyncio.Event):
    # Hard-coded sem=1: never fire two subscriptions simultaneously.
    # parser.py also holds its own lock, so this is belt-and-suspenders.
    logger.info("Scheduler started (per-sub interval=60s, sem=1, no stagger)")
    sem = asyncio.Semaphore(1)
    tasks: dict[int, asyncio.Task] = {}

    while not stop_event.is_set():
        try:
            subs = await db.get_active_subscriptions()
        except Exception as e:
            logger.error("Failed to load subs: %s", e)
            subs = []

        active_ids = {s["id"] for s in subs}

        # Cancel tasks for removed/finished subscriptions
        for sid in list(tasks):
            if sid not in active_ids or tasks[sid].done():
                if not tasks[sid].done():
                    tasks[sid].cancel()
                tasks.pop(sid, None)

        # Spawn new tasks
        for sub in subs:
            if sub["id"] not in tasks:
                tasks[sub["id"]] = asyncio.create_task(
                    _sub_loop(dict(sub), bot, sem, stop_event)
                )

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30)
            break
        except asyncio.TimeoutError:
            pass

    # Shutdown: cancel all subscription tasks
    logger.info("Scheduler stopping, cancelling %d tasks", len(tasks))
    for t in tasks.values():
        t.cancel()
    for t in tasks.values():
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    logger.info("Scheduler stopped")


async def _sub_loop(sub: dict, bot: Bot, sem: asyncio.Semaphore, stop_event: asyncio.Event):
    """One loop per subscription. Uses a global semaphore (1) so only one
    subscription fetches at a time. No startup or inter-request stagger —
    60-s cycle spacing is all we need."""
    logger.info("Sub #%d loop started", sub["id"])

    consecutive_failures = 0

    while not stop_event.is_set():
        try:
            # Always re-read URL from DB — never trust the snapshot we got
            # at task spawn time. User may have edited / re-added the
            # subscription with a longer/cleaner URL.
            fresh = await db.get_subscription(sub["id"])
            if fresh is None:
                logger.info("Sub #%d deleted/deactivated — exiting loop", sub["id"])
                return
            sub["url"] = fresh["url"]
            sub["last_checked_at"] = fresh["last_checked_at"]
            sub["telegram_id"] = fresh["telegram_id"]

            async with sem:
                _url = sub["url"]
                src = detect_source(_url)
                source_name = src.name if src else ""
                proxy = proxy_for_source(source_name)
                logger.info(
                    "[scheduler] Sub #%d cycle: source=%s proxy=%s url='%s...%s' (len=%d)",
                    sub["id"], source_name or "?", "mobile" if proxy else "direct",
                    _url[:80], _url[-30:], len(_url),
                )
                items = await fetch_search_items(_url, proxy)

            if items is None:
                consecutive_failures += 1
                logger.warning(
                    "Sub #%d: parse failed (%d in a row)",
                    sub["id"], consecutive_failures,
                )
                await db.increment_error(sub["id"], "Parse failed")

                if consecutive_failures >= 3:
                    logger.warning(
                        "Sub #%d: 3 consecutive failures — pausing for 10 minutes",
                        sub["id"],
                    )
                    consecutive_failures = 0
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=600)
                        break
                    except asyncio.TimeoutError:
                        continue  # 10 min passed — start fresh cycle
            else:
                consecutive_failures = 0
                await _process_items(sub, items, bot)
                await db.update_last_checked(sub["id"])
                sub["last_checked_at"] = datetime.now(timezone.utc)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Sub #%d loop error", sub["id"])
            try:
                await db.increment_error(sub["id"], str(e)[:200])
            except Exception:
                pass

        # Fixed 60s between cycles — user requirement.
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Sub #%d loop stopped", sub["id"])


async def _process_items(sub: dict, items: list[SearchItem], bot: Bot):
    is_first_scan = sub.get("last_checked_at") is None

    # Group by source so a single batch insert can be made even if a
    # subscription accidentally returns items from more than one site
    # (shouldn't happen, but harmless safety).
    if is_first_scan:
        by_source: dict[str, list[str]] = {}
        for i in items:
            if i.external_id:
                by_source.setdefault(i.source, []).append(i.external_id)
        for src, ids in by_source.items():
            await db.mark_items_sent_batch(sub["id"], ids, source=src)
        total = sum(len(v) for v in by_source.values())
        logger.info("Sub #%d first scan: marked %d items as seen", sub["id"], total)
        return

    # Find new items (not yet in sent_items)
    new_items: list[SearchItem] = []
    for item in items:
        if not item.external_id:
            continue
        if not await db.is_item_sent(sub["id"], item.external_id, source=item.source):
            new_items.append(item)

    if not new_items:
        return

    now_ts = int(time.time())
    cutoff = now_ts - MAX_AGE_SECONDS
    fresh = [
        i for i in new_items
        if not i.published_timestamp or i.published_timestamp >= cutoff
    ]

    to_send = fresh[:MAX_ITEMS_PER_CYCLE]

    logger.info(
        "Sub #%d: %d new, %d fresh, %d to send",
        sub["id"], len(new_items), len(fresh), len(to_send),
    )

    # Per-user translation: each subscription belongs to one user, who
    # picked a target language at /start. Translation happens inside
    # _send_notification with a shared cache keyed by (text_hash, lang),
    # so multiple subscriptions in the same language reuse work.
    prefs = await db.get_user_prefs_by_telegram(sub["telegram_id"])
    sent_keys: set[tuple[str, str]] = set()
    for item in to_send:
        try:
            await _send_notification(bot, sub, item, prefs)
            sent_keys.add((item.source, item.external_id))
            await db.mark_item_sent(sub["id"], item.external_id, source=item.source)
        except Exception as e:
            logger.warning("Send failed for %s/%s: %s", item.source, item.external_id, e)
        await asyncio.sleep(0.5)

    # Mark leftovers as seen so we don't re-process them next cycle
    leftover_by_source: dict[str, list[str]] = {}
    for i in new_items:
        key = (i.source, i.external_id)
        if i.external_id and key not in sent_keys:
            leftover_by_source.setdefault(i.source, []).append(i.external_id)
    for src, ids in leftover_by_source.items():
        await db.mark_items_sent_batch(sub["id"], ids, source=src)


# Sources whose text is NOT already in the user's language by default.
# Avito is RU-native; Kufar (Belarus) ships mixed RU/BE; everything else
# uses the seller's native language. We feed source="auto" to Google so
# it detects each item separately rather than guessing per source.
_TRANSLATED_SOURCES: frozenset[str] = frozenset(
    {"olx", "vinted", "mercari", "avito", "kufar", "goofish"}
)

# When the user picked a language that already matches the item's
# native language we skip the Google round-trip. Mapping is best-effort;
# missing entries fall through to "translate anyway" (Google no-ops same-
# language calls cheaply).
_SOURCE_NATIVE_LANG: dict[str, str] = {
    "avito":   "ru",
    "kufar":   "ru",
    "mercari": "ja",
    "goofish": "zh",
}

# Cap concurrent Google Translate calls so a batch of 10 new items
# doesn't fire 20 parallel requests and get rate-limited into 429s.
_TRANSLATE_SEM = asyncio.Semaphore(4)

# Process-local LRU-ish cache: (text_hash, target_lang) → translated.
# Cleared crudely when it grows past the cap. Different users on the
# same language share entries, which is the whole point of the cache.
_TRANSLATE_CACHE: dict[tuple[int, str], str] = {}
_TRANSLATE_CACHE_MAX = 4000


async def _translate(text: str, target_lang: str) -> str:
    """Translate `text` into `target_lang` (ISO-639-1). Returns the
    original text unchanged on empty input or any library error.

    Result is memoised by (hash(text), target_lang) so different items
    quoting the same condition string ("Nuevo con etiquetas") only call
    Google once per language.
    """
    if not text or not text.strip() or not target_lang:
        return text or ""
    key = (hash(text), target_lang)
    cached = _TRANSLATE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        logger.debug("[translate] deep_translator not installed")
        return text

    def _sync_translate() -> str | None:
        try:
            return GoogleTranslator(source="auto", target=target_lang).translate(
                text[:2500],
            )
        except Exception as e:
            logger.debug("[translate] google err: %s", str(e)[:120])
            return None

    loop = asyncio.get_running_loop()
    async with _TRANSLATE_SEM:
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _sync_translate), timeout=6.0,
            )
        except asyncio.TimeoutError:
            logger.debug("[translate] timeout after 6s")
            return text
        except Exception as e:
            logger.debug("[translate] executor err: %s", e)
            return text
    if not isinstance(result, str) or not result.strip():
        return text
    if len(_TRANSLATE_CACHE) > _TRANSLATE_CACHE_MAX:
        # Crude eviction: drop the oldest half. Order is insertion order
        # in CPython 3.7+, which is good enough for an LRU approximation.
        keys = list(_TRANSLATE_CACHE.keys())[: _TRANSLATE_CACHE_MAX // 2]
        for k in keys:
            _TRANSLATE_CACHE.pop(k, None)
    _TRANSLATE_CACHE[key] = result
    return result


async def _localise_item(
    item: SearchItem, target_lang: str,
) -> tuple[str, str | None, str | None]:
    """Return (title, description, condition) translated into target_lang.

    The original `item` is left unchanged so the same item can be sent
    to multiple users in different languages from the same scheduler
    cycle without cross-talk."""
    if item.source not in _TRANSLATED_SOURCES:
        return item.title, item.description, item.condition
    native = _SOURCE_NATIVE_LANG.get(item.source)
    # Skip the round-trip when the source language already matches the
    # user's pick (RU user reading Avito etc.). Catches the common case;
    # auto-detect handles the rest.
    if native and native == target_lang:
        return item.title, item.description, item.condition

    tasks = []
    fields: list[str] = []
    if item.title:
        tasks.append(_translate(item.title, target_lang)); fields.append("title")
    if item.description:
        tasks.append(_translate(item.description, target_lang)); fields.append("desc")
    if item.condition:
        tasks.append(_translate(item.condition, target_lang)); fields.append("cond")
    if not tasks:
        return item.title, item.description, item.condition
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        logger.debug("[translate] gather err: %s", e)
        return item.title, item.description, item.condition
    out_title, out_desc, out_cond = item.title, item.description, item.condition
    for name, r in zip(fields, results):
        if not isinstance(r, str) or not r.strip():
            continue
        if name == "title":
            out_title = r
        elif name == "desc":
            out_desc = r
        elif name == "cond":
            out_cond = r
    return out_title, out_desc, out_cond


def _source_button_text(source: str | None) -> str:
    if not source:
        return "🔗 Открыть объявление"
    return f"🔗 Открыть на {source_display_name(source)}"

# Per-source Referer for image downloads — Avito's CDN refuses requests
# without the Avito Referer; other sites have similar checks.
_SOURCE_IMAGE_REFERER = {
    "avito":   "https://www.avito.ru/",
    "kufar":   "https://www.kufar.by/",
    "olx":     "https://www.olx.com/",
    "vinted":  "https://www.vinted.com/",
    "mercari": "https://jp.mercari.com/",
    "goofish": "https://www.goofish.com/",
}


async def _send_notification(
    bot: Bot, sub: dict, item: SearchItem, prefs: dict,
):
    # Translate per the receiving user's language preference. This lets
    # one scheduler push the same listing in RU to one user and EN to
    # another without re-fetching it. Translation is cached by
    # (text, lang) so a 10-item batch runs ~10 Google calls, not 30.
    lang = prefs.get("lang") or "ru"
    tz = prefs.get("tz") or default_tz_for_lang(lang)
    title, description, condition = await _localise_item(item, lang)
    text = _format_notification(
        item, title=title, description=description, condition=condition,
        user_currency=(prefs.get("currency") or "rub").upper(),
        user_tz=tz,
    )
    button_text = _source_button_text(item.source)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=button_text, url=item.url)],
    ])

    if item.image_url:
        caption = text if len(text) <= 1024 else text[:1020] + "…"
        # Download via the SAME source-host session that already has
        # warm cookies — passing host=item.source avoids spinning up
        # a fresh "generic" session with its own warmup (~7s) on every
        # image. Per-source Referer placates picky CDNs.
        img_bytes = await download_image_bytes(
            item.image_url,
            host=item.source,
            referer=_SOURCE_IMAGE_REFERER.get(item.source),
            proxy=proxy_for_source(item.source),
        )
        if img_bytes:
            try:
                await bot.send_photo(
                    sub["telegram_id"],
                    photo=BufferedInputFile(img_bytes, filename="photo.jpg"),
                    caption=caption, parse_mode="HTML", reply_markup=keyboard,
                )
                return
            except Exception as e:
                logger.info(
                    "send_photo (bytes) failed (%s) for %s/%s — fallback to text",
                    str(e)[:80], item.source, item.external_id,
                )
        else:
            logger.info(
                "[image] could not download %s for %s/%s",
                item.image_url[:80], item.source, item.external_id,
            )

    await bot.send_message(
        sub["telegram_id"], text,
        parse_mode="HTML", reply_markup=keyboard,
        disable_web_page_preview=True,
    )


def _format_notification(
    item: AvitoItem, *,
    title: str | None = None,
    description: str | None = None,
    condition: str | None = None,
    user_currency: str = "RUB",
    user_tz: str = "Europe/Moscow",
) -> str:
    """Unified pretty notification format.

    Layout (missing fields collapse their line):
        <title (size, condition)>
        💰 <native price> (~user-currency estimate)
        📍 <location>
        📅 <сегодня/вчера/DD.MM> в HH:MM (МСК)
        <description>                ← short, max ~180 chars
        👤 <seller>

    `title`, `description`, `condition` come from the per-user
    translation step. When omitted (e.g. unit-test path), the item's
    raw fields are used.
    """
    import html as _html

    raw_title = title if title is not None else item.title
    raw_desc = description if description is not None else item.description
    raw_cond = condition if condition is not None else item.condition

    # Decode HTML entities the seller (or scrape path) left in their
    # text — "Jack &amp; Jones" should render as "Jack & Jones".
    raw_title = _html.unescape(raw_title or "")
    if raw_desc:
        raw_desc = _html.unescape(raw_desc)
    if raw_cond:
        raw_cond = _html.unescape(raw_cond)

    # Append (size, condition) in parens after the (translated) title.
    extras: list[str] = []
    if item.size:
        extras.append(item.size)
    if raw_cond:
        extras.append(raw_cond)
    if extras:
        # Avoid duplicate noise if seller already wrote one of these
        title_lc = raw_title.lower()
        unique = [x for x in extras if x.lower() not in title_lc]
        if unique:
            raw_title = f"{raw_title} ({', '.join(unique)})"

    title_html = _escape(raw_title) or "Без названия"
    price_native = _escape(_format_price(item, user_currency)) or "Цена не указана"
    location = _escape(item.location) or "—"
    when = _format_when_local(item.published_timestamp, user_tz)

    lines = [
        f"<b>{title_html}</b>",
        f"💰 <b>{price_native}</b>",
        f"📍 {location}",
        f"📅 {when}",
    ]

    if raw_desc:
        desc = _prettify_description(raw_desc, _DESC_HARD_MAX)
        if desc:
            # Blank line between header and description — cleaner than a
            # U-bar separator, and leaves room within the 1024-char
            # Telegram photo-caption budget.
            lines.append("")
            lines.append(f"<i>{_escape(desc)}</i>")

    if item.seller_name:
        lines.append(f"\n👤 {_escape(item.seller_name)}")

    return "\n".join(lines)


def _format_price(item: AvitoItem, user_currency: str) -> str:
    """Render the price with a user-currency estimate when conversion is
    possible. Falls back to the parser's raw `item.price` string when
    we don't have the numeric value to convert."""
    if item.price_value and item.currency:
        return format_with_estimate(
            item.price_value, item.currency, user_currency,
            fallback=item.price,
        )
    return item.price or "Цена не указана"


def _format_when_local(ts: int | None, tz_name: str = "Europe/Moscow") -> str:
    """Pretty-print a unix timestamp in the user's timezone with a short
    suffix (МСК / Мадрид / Токио / …). Same-day ads show «Сегодня в
    HH:MM», one-day-old show «Вчера в HH:MM», older ones «DD.MM в HH:MM».

    Falls back to UTC if the IANA name isn't on the system tzdb (which
    shouldn't happen — `tzdata` package is in requirements.txt — but a
    typo in user input shouldn't crash the renderer).
    """
    if not ts:
        return "—"
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
        tz_name = "UTC"
    dt = datetime.fromtimestamp(ts, tz)
    now = datetime.now(tz)
    hhmm = dt.strftime("%H:%M")
    suffix = "UTC" if tz is timezone.utc else timezone_short(tz_name)
    if dt.date() == now.date():
        return f"Сегодня в {hhmm} ({suffix})"
    if dt.date() == (now.date() - timedelta(days=1)):
        return f"Вчера в {hhmm} ({suffix})"
    return f"{dt.strftime('%d.%m')} в {hhmm} ({suffix})"


def _escape(s: str) -> str:
    if not s:
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Compact description — read at a glance, no wall of text in the caption.
# Telegram photo caption limit is 1024; leaving ~240 chars for the
# description keeps titles/prices/seller readable even on longer ads.
_DESC_SOFT_MAX = 160
_DESC_HARD_MAX = 240

# Marketing / boilerplate line-starts to drop. Matched against
# line.lower() with .startswith(). Ordered roughly by frequency.
_FLUFF_PREFIXES = (
    # Polish originals (pre-translation safety net)
    "zapraszamy", "witam", "dzień dobry", "dzien dobry",
    "raty ", "0%", "promocja", "promo ", "negocjuj",
    "sklep stacjonarny", "darmowa dostawa",
    "cena ", "pierwotnie",
    # Russian — after Google-translate these are what the PL/UA/RO
    # boilerplate usually renders as, so the fluff filter still has
    # something to catch post-translation.
    "приглашаем", "добро пожаловать", "здравствуйте", "добрый день",
    "рассрочка", "бесплатная доставка",
    "стационарный магазин", "наш магазин", "наш салон",
    "первоначально", "изначально", "ранее ", "цена снижена",
    "торг ", "торг.", "торг,", "торг!", "договорная",
    # Ukrainian / Romanian / Portuguese / Spanish originals
    "вітаємо", "ласкаво просимо", "знижка",
    "bună ziua", "bine ați venit", "preț ",
    "olá", "bom dia", "seja bem-vindo", "preço",
    "originalmente", "precio original", "antes ",
    "originally", "originally:", "was ", "rrp ",
)


def _is_fluff_line(line: str) -> bool:
    low = line.strip().lower()
    if not low:
        return True
    return any(low.startswith(p) for p in _FLUFF_PREFIXES)


def _prettify_description(raw: str, hard_max: int = _DESC_HARD_MAX) -> str:
    """Clean up a seller's free-form description for a Telegram card.

    Strategy:
      - HTML-decode entities (`&amp;` etc.)
      - strip zero-width / NBSP junk + normalize whitespace while
        PRESERVING line breaks (bullet lists stay readable)
      - drop fluff lines from BOTH top and bottom (sellers stick
        "Originally 80€" / "Negotiable" at the end)
      - cut at the last sentence-end period before hard cap; fall back
        to line break, then word boundary
    """
    import html as _html
    import re as _re
    if not raw:
        return ""
    text = _html.unescape(raw)
    # Drop invisible / bidi chars
    for ch in ("\u200B", "\u200C", "\u200D", "\u2060", "\u00AD",
               "\uFEFF", "\u00A0"):
        text = text.replace(ch, " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse repeated spaces/tabs, strip lead/trail whitespace on
    # each line, cap consecutive blank lines.
    text = _re.sub(r"[ \t]+", " ", text)
    text = _re.sub(r" *\n *", "\n", text)
    text = _re.sub(r"\n{3,}", "\n\n", text)
    text = _re.sub(r"\.\s*,", ".", text)
    text = _re.sub(r",\s*\.", ".", text)
    text = text.strip()
    if not text:
        return ""

    # Drop fluff lines from BOTH ends. Sellers tend to bracket their
    # actual description with "Hi, welcome" at the top and
    # "originally 80€, negotiable" at the bottom.
    lines = text.split("\n")
    while lines and _is_fluff_line(lines[0]):
        lines.pop(0)
    while lines and _is_fluff_line(lines[-1]):
        lines.pop()
    text = "\n".join(lines).strip()
    if not text:
        return ""

    soft_limit = min(_DESC_SOFT_MAX, hard_max)
    if len(text) <= soft_limit:
        return text

    # Prefer the latest *sentence-end* boundary before the hard cap —
    # `\n\n` before a fluff trailer used to win over a clean period
    # earlier in the text, leaving an awkward "Originally:..." dangling.
    window = text[:hard_max]
    sentence_ends = [
        window.rfind(m) for m in (". ", "! ", "? ", ".\n", "!\n", "?\n")
    ]
    sentence_ends.append(window.rfind("."))  # text ending with a period
    viable_sent = [b for b in sentence_ends if b >= soft_limit // 2]
    if viable_sent:
        cut_at = max(viable_sent) + 1  # include the terminator itself
        return text[:cut_at].rstrip()

    # No sentence end in range — fall back to a line break.
    line_breaks = [window.rfind(m) for m in ("\n\n", "\n")]
    viable_line = [b for b in line_breaks if b >= soft_limit // 2]
    if viable_line:
        out = text[: max(viable_line)].rstrip()
        if not out.endswith((".", "!", "?", "…")):
            out += "…"
        return out

    # No natural boundary — word trim with «…»
    cut = window
    sp = cut.rfind(" ")
    if sp > hard_max // 2:
        cut = cut[:sp]
    return cut.rstrip() + "…"


