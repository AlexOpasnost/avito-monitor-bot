import asyncio
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import db
from parser import parse_listings, enrich_item, AvitoItem, _get_proxy
from config import config

logger = logging.getLogger(__name__)


def format_notification(item: AvitoItem) -> str:
    """Format item notification — AvtoRinger style."""
    lines = []

    # Line 1: Title + Price + Stats
    line1 = f"<b>{item.title}</b> 💰 <b>{item.price}</b>"
    stats = []
    if item.views is not None:
        stats.append(f"👀 {item.views}")
    if item.favorites is not None:
        stats.append(f"❤️ {item.favorites}")
    if item.seller_rating:
        r = f"⭐ {item.seller_rating}"
        if item.seller_reviews:
            r += f" ({item.seller_reviews})"
        stats.append(r)
    if stats:
        line1 += " " + " ".join(stats)
    lines.append(line1)

    # Location
    if item.location:
        lines.append(f"📍 {item.location}")

    # Link
    lines.append(f"avito.ru/{item.avito_id}")

    # Description
    if item.description:
        lines.append(f"\n<i>{item.description}</i>")

    # Seller
    if item.seller_name:
        lines.append(f"\n👤 {item.seller_name}")

    # Date
    if item.published_date:
        lines.append(f"📅 {item.published_date}")

    # ID
    lines.append(f"🆔 <code>{item.avito_id}</code>")

    return "\n".join(lines)


def make_item_keyboard(item: AvitoItem) -> InlineKeyboardMarkup:
    short_url = f"https://www.avito.ru/{item.avito_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Открыть на Авито", url=short_url)],
    ])


async def check_subscription(bot: Bot, sub: dict):
    sub_id = sub["id"]
    telegram_id = sub["telegram_id"]
    url = sub["url"]

    items = await parse_listings(url)

    if items is None:
        deactivated = await db.increment_error(sub_id, "Parse failed or blocked")
        if deactivated:
            try:
                await bot.send_message(
                    telegram_id,
                    f"⚠️ Отслеживание #{sub_id} остановлено — слишком много ошибок.",
                )
            except Exception:
                pass
        return

    await db.reset_errors(sub_id)

    enrich_ok = True  # disable enrich if first attempt fails
    new_count = 0
    for item in items:
        if not item.avito_id:
            continue

        # Check if still active (user may have /delete'd)
        still_active = await db.is_subscription_active(sub_id)
        if not still_active:
            logger.info("Sub #%d deactivated, stopping", sub_id)
            return

        already_sent = await db.is_item_sent(sub_id, item.avito_id)
        if already_sent:
            continue

        # Enrich — rotate IP first, then try once
        if enrich_ok:
            try:
                # Rotate IP before enrich (list API already used current IP)
                if config.proxy_rotate_url:
                    try:
                        import aiohttp as _aio
                        _t = _aio.ClientTimeout(total=10)
                        async with _aio.ClientSession(timeout=_t) as _s:
                            async with _s.get(config.proxy_rotate_url) as _r:
                                pass
                        await asyncio.sleep(5)
                    except Exception:
                        pass

                proxy = _get_proxy()
                loop = asyncio.get_event_loop()
                enriched = await loop.run_in_executor(None, lambda: enrich_item(item, proxy))
                if enriched.description or enriched.seller_name:
                    item = enriched
                    logger.info("Enriched item %s with description", item.avito_id)
                else:
                    enrich_ok = False
                    logger.info("Enrich returned no data, disabling for cycle")
            except Exception:
                enrich_ok = False

        # Re-check active right before sending
        still_active2 = await db.is_subscription_active(sub_id)
        if not still_active2:
            logger.info("Sub #%d deactivated before send, stopping", sub_id)
            return

        text = format_notification(item)
        keyboard = make_item_keyboard(item)

        # Send with retry: photo first, text fallback
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
            # Rotate IP before each cycle
            if config.proxy_rotate_url:
                try:
                    import aiohttp as _aiohttp
                    timeout = _aiohttp.ClientTimeout(total=15)
                    async with _aiohttp.ClientSession(timeout=timeout) as _sess:
                        async with _sess.get(config.proxy_rotate_url) as _resp:
                            if _resp.status == 200:
                                logger.info("IP rotated")
                            await asyncio.sleep(5)
                except Exception as e:
                    logger.warning("IP rotation failed: %s", e)

            subs = await db.get_active_subscriptions()
            if subs:
                logger.info("Checking %d subscriptions", len(subs))
                for sub in subs:
                    if stop_event.is_set():
                        break
                    try:
                        await check_subscription(bot, sub)
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
