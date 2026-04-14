"""Per-subscription async scheduler."""
import asyncio
import logging
import random
import re
import time
from datetime import datetime, timezone, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from database import db
from parser import AvitoItem, extract_price_range, fetch_search_items

logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))
MAX_ITEMS_PER_CYCLE = 10
MAX_AGE_SECONDS = 2 * 24 * 3600  # 2 days


async def run_scheduler(bot: Bot, stop_event: asyncio.Event):
    logger.info(
        "Scheduler started (interval=%ds, max_concurrent=%d)",
        config.parse_interval, config.max_concurrent_requests,
    )

    sem = asyncio.Semaphore(config.max_concurrent_requests)
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
    """One loop per subscription."""
    # Stagger startup so we don't hit Avito from all tasks at once
    await asyncio.sleep(random.uniform(0, 30))
    logger.info("Sub #%d loop started", sub["id"])

    while not stop_event.is_set():
        try:
            proxy = config.proxy_list[0] if config.proxy_list else None
            async with sem:
                items = await fetch_search_items(sub["url"], proxy)

            if items is None:
                logger.warning("Sub #%d: parse failed (None)", sub["id"])
                await db.increment_error(sub["id"], "Parse failed")
            else:
                await _process_items(sub, items, bot)
                await db.update_last_checked(sub["id"])
                # Update in-memory last_checked_at so next cycle is not a "first scan"
                sub["last_checked_at"] = datetime.now(timezone.utc)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Sub #%d loop error", sub["id"])
            try:
                await db.increment_error(sub["id"], str(e)[:200])
            except Exception:
                pass

        # Jittered sleep
        wait = max(10, config.parse_interval + random.uniform(-8, 8))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Sub #%d loop stopped", sub["id"])


async def _process_items(sub: dict, items: list[AvitoItem], bot: Bot):
    is_first_scan = sub.get("last_checked_at") is None

    # Client-side safety net: drop anything whose price is outside the
    # range specified in the user's URL. Avito's server should already
    # honor this but we double-check to guarantee no out-of-range items.
    pmin, pmax = extract_price_range(sub["url"])
    if pmin is not None or pmax is not None:
        before = len(items)
        items = [i for i in items if _price_in_range(i, pmin, pmax)]
        dropped = before - len(items)
        if dropped:
            logger.info(
                "Sub #%d: dropped %d items outside price range [%s, %s]",
                sub["id"], dropped, pmin, pmax,
            )

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
    if item.image_url:
        caption = text if len(text) <= 1024 else text[:1020] + "..."
        try:
            await bot.send_photo(
                sub["telegram_id"], photo=item.image_url,
                caption=caption, parse_mode="HTML", reply_markup=keyboard,
            )
            return
        except Exception as e:
            logger.debug("send_photo failed (%s), fallback to text", e)

    await bot.send_message(
        sub["telegram_id"], text,
        parse_mode="HTML", reply_markup=keyboard,
        disable_web_page_preview=True,
    )


def _format_notification(item: AvitoItem) -> str:
    lines = [f"<b>{_escape(item.title)}</b>"]
    lines.append(f"💰 <b>{_escape(item.price)}</b>")
    if item.location:
        lines.append(f"📍 {_escape(item.location)}")
    if item.description:
        lines.append(f"\n<i>{_escape(item.description)}</i>")
    if item.seller_name:
        lines.append(f"\n👤 {_escape(item.seller_name)}")
    if item.published_timestamp:
        dt = datetime.fromtimestamp(item.published_timestamp, MSK)
        lines.append(f"📅 {dt.strftime('%H:%M %d.%m.%Y')}")
    return "\n".join(lines)


def _escape(s: str) -> str:
    if not s:
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _price_in_range(item: AvitoItem, pmin: int | None, pmax: int | None) -> bool:
    """Return True if the item should pass the price filter.

    Items with no parseable price are kept — we can't know whether they
    fit and dropping them would cause false negatives on services /
    listings where the price is negotiable."""
    price = item.price_value
    if price is None:
        # Try to pull a number out of the price string as a fallback
        if item.price:
            digits = re.sub(r"\D+", "", item.price)
            if digits:
                try:
                    price = int(digits)
                except ValueError:
                    price = None
    if price is None:
        return True  # unknown price — don't drop
    if pmin is not None and price < pmin:
        return False
    if pmax is not None and price > pmax:
        return False
    return True
