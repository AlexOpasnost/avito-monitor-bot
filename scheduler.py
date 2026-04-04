import asyncio
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import db
from parser import parse_listings, enrich_item, AvitoItem
from config import config

logger = logging.getLogger(__name__)


def _clean_url(url: str) -> str:
    """Ensure URL is valid for Telegram buttons."""
    if not url:
        return "https://www.avito.ru"
    if not url.startswith("http"):
        url = "https://www.avito.ru" + url
    # Remove query params
    if "?" in url:
        url = url.split("?")[0]
    return url


def format_notification(item: AvitoItem) -> str:
    """Format item notification — AvtoRinger style."""
    url = _clean_url(item.url)

    lines = []
    lines.append(f"<b>{item.title}</b>")
    lines.append(f"💰 <b>{item.price}</b>")

    # Stats line
    stats = []
    if item.views:
        stats.append(f"👁 {item.views}")
    if item.seller_rating:
        stats.append(f"⭐ {item.seller_rating}")
    if stats:
        lines.append(" ".join(stats))

    if item.location:
        lines.append(f"📍 {item.location}")

    lines.append(f"🔗 <a href=\"{url}\">{url.split('/')[-1][:40]}</a>")

    if item.description:
        lines.append(f"\n<i>{item.description}</i>")

    if item.seller_name:
        seller = item.seller_name
        if seller.lower() not in ("подписаться", "профиль"):
            lines.append(f"👤 {seller}")

    if item.published_date:
        lines.append(f"📅 {item.published_date}")

    return "\n".join(lines)


def make_item_keyboard(item: AvitoItem) -> InlineKeyboardMarkup:
    url = _clean_url(item.url)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Открыть на Авито", url=url)],
    ])


async def notify_subscription(bot: Bot, sub: dict, items: list[AvitoItem]):
    """Send new items to a single subscription."""
    sub_id = sub["id"]
    telegram_id = sub["telegram_id"]
    is_first_scan = sub.get("last_checked_at") is None

    # First scan: mark all existing items as seen WITHOUT sending
    if is_first_scan:
        for item in items:
            if item.avito_id:
                await db.mark_item_sent(sub_id, item.avito_id)
        logger.info("First scan for sub #%d: marked %d items as seen (no notifications)",
                     sub_id, len(items))
        return

    new_count = 0
    for item in items:
        if not item.avito_id:
            continue

        already_sent = await db.is_item_sent(sub_id, item.avito_id)
        if already_sent:
            continue

        still_active = await db.is_subscription_active(sub_id)
        if not still_active:
            logger.info("Sub #%d deactivated, stopping", sub_id)
            return

        # Fetch full details from item page (date, description, views, seller)
        item = await enrich_item(item)
        await asyncio.sleep(1)  # Don't hammer Avito

        text = format_notification(item)
        keyboard = make_item_keyboard(item)

        sent = False
        for attempt in range(2):
            try:
                if item.image_url and attempt == 0:
                    caption = text if len(text) <= 1024 else text[:1020] + "..."
                    await bot.send_photo(
                        telegram_id, photo=item.image_url,
                        caption=caption, parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                else:
                    await bot.send_message(
                        telegram_id, text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup=keyboard,
                    )
                sent = True
                break
            except Exception as e:
                logger.warning("Send attempt %d failed: %s", attempt + 1, e)
                if attempt == 0:
                    await asyncio.sleep(0.5)

        if sent:
            await db.mark_item_sent(sub_id, item.avito_id)
            new_count += 1

    if new_count > 0:
        logger.info("Sent %d new items for sub #%d", new_count, sub_id)


async def run_scheduler(bot: Bot, stop_event: asyncio.Event):
    logger.info("Scheduler started (interval: %ds)", config.parse_interval)
    await asyncio.sleep(5)

    while not stop_event.is_set():
        try:
            subs = await db.get_active_subscriptions()
            if subs:
                # Deduplicate: group subscriptions by URL, parse each URL once
                from collections import defaultdict
                url_groups: dict[str, list[dict]] = defaultdict(list)
                for sub in subs:
                    url_groups[sub["url"]].append(sub)

                logger.info(
                    "Checking %d subscriptions (%d unique URLs)",
                    len(subs), len(url_groups),
                )

                for url, group_subs in url_groups.items():
                    if stop_event.is_set():
                        break

                    items = await parse_listings(url)

                    if items is None:
                        for sub in group_subs:
                            try:
                                deactivated = await db.increment_error(
                                    sub["id"], "Parse failed or blocked"
                                )
                                if deactivated:
                                    try:
                                        await bot.send_message(
                                            sub["telegram_id"],
                                            f"⚠️ Отслеживание #{sub['id']} остановлено — слишком много ошибок.",
                                        )
                                    except Exception:
                                        pass
                            except Exception as e:
                                logger.error("Error incrementing error sub #%d: %s", sub["id"], e)
                        continue

                    for sub in group_subs:
                        if stop_event.is_set():
                            break
                        try:
                            await db.reset_errors(sub["id"])
                            await notify_subscription(bot, sub, items)
                        except Exception as e:
                            logger.error("Error sub #%d: %s", sub["id"], e)
        except Exception as e:
            logger.error("Scheduler error: %s", e)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.parse_interval)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Scheduler stopped")
