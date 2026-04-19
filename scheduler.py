"""Per-subscription async scheduler."""
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from database import db
from parser import AvitoItem, fetch_search_items

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


async def _process_items(sub: dict, items: list[AvitoItem], bot: Bot):
    is_first_scan = sub.get("last_checked_at") is None

    # Trust Avito 100% — items already match the URL's filters server-side.
    # We do NOT do client-side price/category/keyword filtering.

    if is_first_scan:
        ids = [i.avito_id for i in items if i.avito_id]
        await db.mark_items_sent_batch(sub["id"], ids)
        logger.info("Sub #%d first scan: marked %d items as seen", sub["id"], len(ids))
        return

    # Find new items
    new_items: list[AvitoItem] = []
    for item in items:
        if not item.avito_id:
            continue
        if not await db.is_item_sent(sub["id"], item.avito_id):
            new_items.append(item)

    if not new_items:
        return

    # Filter by age (skip items older than 2 days)
    now_ts = int(time.time())
    cutoff = now_ts - MAX_AGE_SECONDS
    fresh = [
        i for i in new_items
        if not i.published_timestamp or i.published_timestamp >= cutoff
    ]

    # Limit per cycle
    to_send = fresh[:MAX_ITEMS_PER_CYCLE]

    logger.info(
        "Sub #%d: %d new, %d fresh, %d to send",
        sub["id"], len(new_items), len(fresh), len(to_send),
    )

    sent_ids: set[str] = set()
    for item in to_send:
        try:
            await _send_notification(bot, sub, item)
            sent_ids.add(item.avito_id)
            await db.mark_item_sent(sub["id"], item.avito_id)
        except Exception as e:
            logger.warning("Send failed for %s: %s", item.avito_id, e)
        await asyncio.sleep(0.5)

    # Mark everything else (too old or beyond the per-cycle limit) as seen
    leftover_ids = [
        i.avito_id for i in new_items
        if i.avito_id and i.avito_id not in sent_ids
    ]
    if leftover_ids:
        await db.mark_items_sent_batch(sub["id"], leftover_ids)


async def _send_notification(bot: Bot, sub: dict, item: AvitoItem):
    text = _format_notification(item)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Открыть на Авито", url=item.url)],
    ])

    # Try photo first — Telegram renders the image above the caption
    # which gives the nicest look. Fall back to text-only on any error
    # (bad image URL, Telegram refusing the URL, etc.).
    if item.image_url:
        caption = text if len(text) <= 1024 else text[:1020] + "…"
        try:
            await bot.send_photo(
                sub["telegram_id"], photo=item.image_url,
                caption=caption, parse_mode="HTML", reply_markup=keyboard,
            )
            return
        except Exception as e:
            logger.info(
                "send_photo failed (%s) — falling back to text for %s",
                str(e)[:100], item.avito_id,
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
        desc = _escape(item.description).strip()
        if len(desc) > DESC_MAX:
            desc = desc[: DESC_MAX - 1].rstrip() + "…"
        lines.append("──────────────")
        lines.append(f"<i>{desc}</i>")

    if item.seller_name:
        lines.append(f"\n👤 {_escape(item.seller_name)}")

    return "\n".join(lines)


def _escape(s: str) -> str:
    if not s:
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


