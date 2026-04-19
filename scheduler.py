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
    download_image_bytes,
    fetch_search_items,
)

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
                proxy = config.proxy_list[0] if config.proxy_list else None
                _url = sub["url"]
                logger.info(
                    "[scheduler] Sub #%d cycle: using url='%s...%s' (len=%d)",
                    sub["id"], _url[:80], _url[-30:], len(_url),
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

    Layout (always same order, missing fields collapse their line):
        <title>
        💰 <price>
        📍 <location>
        📅 <HH:MM DD.MM.YYYY MSK>
        ──────────────
        <description>
        👤 <seller>
    """
    title = _escape(item.title) or "Без названия"
    price = _escape(item.price) or "Цена не указана"
    location = _escape(item.location) or "—"

    if item.published_timestamp:
        dt = datetime.fromtimestamp(item.published_timestamp, MSK)
        when = dt.strftime("%H:%M %d.%m.%Y")
    else:
        when = "—"

    # Telegram photo caption limit is 1024 chars — leave room for the
    # header so description has predictable budget.
    HEADER_BUDGET = 260  # title + price + location + date + separator + small margin
    DESC_MAX = 1024 - HEADER_BUDGET  # ≈ 764 chars

    lines = [
        f"<b>{title}</b>",
        f"💰 <b>{price}</b>",
        f"📍 {location}",
        f"📅 {when}",
    ]

    if item.description:
        desc = _prettify_description(item.description, DESC_MAX)
        if desc:
            lines.append("──────────────")
            lines.append(f"<i>{_escape(desc)}</i>")

    if item.seller_name:
        lines.append(f"\n👤 {_escape(item.seller_name)}")

    return "\n".join(lines)


def _escape(s: str) -> str:
    if not s:
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Soft max per card — keep descriptions compact so the notification
# reads at a glance. Hard cap passed in if the card is tighter.
_DESC_SOFT_MAX = 400


def _prettify_description(raw: str, hard_max: int) -> str:
    """Clean up a seller's free-form description for a Telegram card.

    - normalizes whitespace (no tripled newlines, no double spaces)
    - strips zero-width / soft-hyphen / NBSP junk
    - collapses "word.,word" typos
    - truncates at sentence boundary near the soft limit, fallback to
      hard_max with ellipsis
    """
    import re as _re
    if not raw:
        return ""
    text = raw
    # Drop invisible / bidi chars that occasionally sneak into
    # copy-pasted descriptions and make them look dirty.
    for ch in ("\u200B", "\u200C", "\u200D", "\u2060", "\u00AD",
               "\uFEFF", "\u00A0"):
        text = text.replace(ch, " ")
    # Windows line endings -> \n
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse 3+ newlines to 2 (paragraph), 2 spaces to 1
    text = _re.sub(r"\n{3,}", "\n\n", text)
    text = _re.sub(r"[ \t]{2,}", " ", text)
    # "word.," / "word. ," punctuation glitches
    text = _re.sub(r"\.\s*,", ".", text)
    text = _re.sub(r",\s*\.", ".", text)
    # Strip trailing whitespace on each line
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = text.strip()

    if not text:
        return ""

    soft_limit = min(_DESC_SOFT_MAX, hard_max)
    if len(text) <= soft_limit:
        return text

    # Try to cut at sentence boundary near the soft limit
    window = text[: soft_limit]
    sentence_end = max(
        window.rfind(". "), window.rfind("! "),
        window.rfind("? "), window.rfind(".\n"),
        window.rfind("!\n"), window.rfind("?\n"),
    )
    if sentence_end >= soft_limit // 2:
        return text[: sentence_end + 1].rstrip() + " …"

    # Fall back to word boundary near hard_max
    if len(text) > hard_max:
        text = text[: hard_max - 2]
        sp = text.rfind(" ")
        if sp > hard_max // 2:
            text = text[:sp]
        return text.rstrip() + "…"

    # Between soft and hard: end at last space
    cut = text[:soft_limit]
    sp = cut.rfind(" ")
    if sp > soft_limit // 2:
        cut = cut[:sp]
    return cut.rstrip() + " …"


