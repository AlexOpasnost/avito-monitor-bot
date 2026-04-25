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
from parsers.common import proxy_for_source

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

    # Translate all to-send items in parallel before the send loop —
    # Google Translate latency is ~500-1500 ms/call, so doing it
    # sequentially per item stretched a 10-item batch to 15+ seconds.
    # Shared _TRANSLATE_SEM (4) keeps us under Google's rate limit.
    foreign_items = [i for i in to_send if i.source in _TRANSLATED_SOURCES]
    if foreign_items:
        try:
            await asyncio.gather(
                *(_russify_item(i) for i in foreign_items),
                return_exceptions=True,
            )
        except Exception as e:
            logger.debug("[translate] bulk gather err: %s", e)

    sent_keys: set[tuple[str, str]] = set()
    for item in to_send:
        try:
            await _send_notification(bot, sub, item)
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


# Sources whose text is NOT already Russian — we'll translate title +
# description via Google Translate before formatting the notification.
# Avito is native RU, Kufar (Belarus) is mostly RU. Everyone else gets
# translated so Russian audience can actually read the card.
_TRANSLATED_SOURCES: frozenset[str] = frozenset({"olx", "vinted", "mercari"})

# Cap concurrent Google Translate calls so a batch of 10 new items
# doesn't fire 20 parallel requests and get rate-limited into 429s.
_TRANSLATE_SEM = asyncio.Semaphore(4)


async def _translate_to_russian(text: str) -> str | None:
    """Translate a short string to Russian via deep_translator's Google
    backend. Returns None on empty input, timeout, or any library error —
    callers should fall back to the original text."""
    if not text or not text.strip():
        return None
    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        logger.debug("[translate] deep_translator not installed")
        return None

    def _sync_translate() -> str | None:
        try:
            # 2500-char cap is well under Google's 5000 hard limit and
            # keeps latency predictable.
            return GoogleTranslator(source="auto", target="ru").translate(
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
            return None
        except Exception as e:
            logger.debug("[translate] executor err: %s", e)
            return None
    if not isinstance(result, str) or not result.strip():
        return None
    return result


async def _russify_item(item: SearchItem) -> None:
    """Translate title + description on foreign-source items. Mutates
    `item` in place; on any failure the original text is preserved so
    the notification still goes out."""
    if item.source not in _TRANSLATED_SOURCES:
        return
    # Parallelize title + description; both calls are usually sub-second
    title_task = _translate_to_russian(item.title) if item.title else None
    desc_task = _translate_to_russian(item.description) if item.description else None
    if title_task is None and desc_task is None:
        return
    tasks = [t for t in (title_task, desc_task) if t is not None]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        logger.debug("[translate] gather err: %s", e)
        return
    idx = 0
    if title_task is not None:
        r = results[idx]
        idx += 1
        if isinstance(r, str) and r.strip():
            item.title = r
    if desc_task is not None:
        r = results[idx]
        if isinstance(r, str) and r.strip():
            item.description = r


_SOURCE_BUTTON_TEXT = {
    "avito":   "🔗 Открыть на Авито",
    "kufar":   "🔗 Открыть на Kufar",
    "olx":     "🔗 Открыть на OLX",
    "vinted":  "🔗 Открыть на Vinted",
    "mercari": "🔗 Открыть на Mercari",
    "goofish": "🔗 Открыть на Goofish",
}

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


async def _send_notification(bot: Bot, sub: dict, item: SearchItem):
    # Translation is done in bulk by _process_items BEFORE this loop
    # starts — here the item is already Russian (or the translator
    # failed and we're showing the original, which is still safe).
    text = _format_notification(item)
    button_text = _SOURCE_BUTTON_TEXT.get(item.source, "🔗 Открыть объявление")
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


def _format_notification(item: AvitoItem) -> str:
    """Unified pretty notification format.

    Layout (missing fields collapse their line):
        <title>
        💰 <price>
        📍 <location>
        📅 <сегодня/вчера/DD.MM> в HH:MM (МСК)
        <description>                ← short, max ~180 chars
        👤 <seller>
    """
    title = _escape(item.title) or "Без названия"
    price = _escape(item.price) or "Цена не указана"
    location = _escape(item.location) or "—"
    when = _format_when_msk(item.published_timestamp)

    lines = [
        f"<b>{title}</b>",
        f"💰 <b>{price}</b>",
        f"📍 {location}",
        f"📅 {when}",
    ]

    if item.description:
        desc = _prettify_description(item.description, _DESC_HARD_MAX)
        if desc:
            # Blank line between header and description — cleaner than a
            # U-bar separator, and leaves room within the 1024-char
            # Telegram photo-caption budget.
            lines.append("")
            lines.append(f"<i>{_escape(desc)}</i>")

    if item.seller_name:
        lines.append(f"\n👤 {_escape(item.seller_name)}")

    return "\n".join(lines)


def _format_when_msk(ts: int | None) -> str:
    """Pretty-print a unix timestamp in Moscow local time with a clear
    МСК suffix. Same-day ads show «Сегодня в HH:MM», one-day-old show
    «Вчера в HH:MM», older ones show «DD.MM в HH:MM»."""
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts, MSK)
    now = datetime.now(MSK)
    hhmm = dt.strftime("%H:%M")
    if dt.date() == now.date():
        return f"Сегодня в {hhmm} (МСК)"
    if dt.date() == (now.date() - timedelta(days=1)):
        return f"Вчера в {hhmm} (МСК)"
    return f"{dt.strftime('%d.%m')} в {hhmm} (МСК)"


def _escape(s: str) -> str:
    if not s:
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Compact description — read at a glance, no wall of text in the caption.
# Telegram photo caption limit is 1024; leaving ~240 chars for the
# description keeps titles/prices/seller readable even on longer ads.
_DESC_SOFT_MAX = 160
_DESC_HARD_MAX = 240

# Marketing / boilerplate line-starts to drop. Strips common "come to
# our store" intros and "installment plan" banners that add no signal.
# Matched against line.lower() with .startswith() for cheapness.
_FLUFF_PREFIXES = (
    # Polish originals (pre-translation safety net)
    "zapraszamy", "witam", "dzień dobry", "dzien dobry",
    "raty ", "0%", "promocja", "promo ",
    "sklep stacjonarny", "darmowa dostawa",
    # Russian — after Google-translate these are what the PL/UA/RO
    # boilerplate usually renders as, so the fluff filter still has
    # something to catch post-translation.
    "приглашаем", "добро пожаловать", "здравствуйте", "добрый день",
    "рассрочка", "бесплатная доставка",
    "стационарный магазин", "наш магазин", "наш салон",
    # Ukrainian / Romanian / Portuguese originals (pre-translation)
    "вітаємо", "ласкаво просимо",
    "bună ziua", "bine ați venit",
    "olá", "bom dia", "seja bem-vindo",
)


def _prettify_description(raw: str, hard_max: int = _DESC_HARD_MAX) -> str:
    """Clean up a seller's free-form description for a Telegram card.

    Strategy:
      - strip zero-width / NBSP junk + normalize whitespace while
        PRESERVING line breaks (bullet lists stay readable)
      - drop leading marketing-boilerplate lines (store invites,
        installment banners, generic greetings)
      - cut at the last natural boundary (\\n, period, !, ?) before the
        hard cap; fall back to word boundary with «…» if none
    """
    import re as _re
    if not raw:
        return ""
    text = raw
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

    # Drop marketing-fluff lines from the top. Only from the top so we
    # don't mangle legit product details mid-description.
    lines = text.split("\n")
    while lines:
        low = lines[0].strip().lower()
        if not low or any(low.startswith(p) for p in _FLUFF_PREFIXES):
            lines.pop(0)
            continue
        break
    text = "\n".join(lines).strip()
    if not text:
        return ""

    soft_limit = min(_DESC_SOFT_MAX, hard_max)
    if len(text) <= soft_limit:
        return text

    # Pick the latest natural break before the hard cap. Preferring
    # line breaks keeps bullet lists intact; period/!/? also work.
    window = text[:hard_max]
    breaks = [window.rfind(m) for m in ("\n\n", "\n", ". ", "! ", "? ",
                                         ".\n", "!\n", "?\n")]
    viable = [b for b in breaks if b >= soft_limit // 2]
    if viable:
        cut_at = max(viable)
        out = text[:cut_at].rstrip()
        # Sentence-terminator cut reads as complete; line-break cut
        # gets an «…» so the reader sees there's more below.
        if not out.endswith((".", "!", "?", "…")):
            out += "…"
        return out

    # No natural boundary — word trim with «…»
    cut = window
    sp = cut.rfind(" ")
    if sp > hard_max // 2:
        cut = cut[:sp]
    return cut.rstrip() + "…"


