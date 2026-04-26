"""Avito Monitor Bot — main entry point."""
import asyncio
import logging
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import BotCommand

from config import config
from database import db
from handlers import router
from parser import check_proxy_ip, rotate_ip
from scheduler import run_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass
logger = logging.getLogger(__name__)


async def main():
    if not config.bot_token:
        logger.error("BOT_TOKEN not set in .env")
        sys.exit(1)

    await db.connect()
    logger.info("Database connected")

    if config.proxy_list:
        logger.info("Proxy configured: %s", config.proxy_list[0].split("@")[-1])
        await rotate_ip()
        ip = await check_proxy_ip()
        logger.info("Proxy IP: %s", ip or "UNKNOWN")
    else:
        logger.warning("No proxy configured — Avito may block requests")

    if config.telegram_api_url and config.telegram_api_url != "https://api.telegram.org":
        session = AiohttpSession(api=TelegramAPIServer.from_base(config.telegram_api_url))
        bot = Bot(token=config.bot_token, session=session)
        logger.info("Using custom Telegram API: %s", config.telegram_api_url)
    else:
        bot = Bot(token=config.bot_token)
        logger.info("Using direct Telegram API")

    dp = Dispatcher()
    dp.include_router(router)

    stop_event = asyncio.Event()
    scheduler_task = asyncio.create_task(run_scheduler(bot, stop_event))

    def shutdown_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, shutdown_handler)

    # Slash-command menu (the blue "Menu" button in Telegram).
    # /settings is folded into /profile (lang/currency live there now)
    # so we don't expose two doors to the same screen.
    await bot.set_my_commands([
        BotCommand(command="start",   description="Главное меню"),
        BotCommand(command="list",    description="Мои поиски"),
        BotCommand(command="profile", description="Профиль и настройки"),
        BotCommand(command="stop",    description="Поставить на паузу"),
        BotCommand(command="help",    description="Как это работает"),
    ])

    # Bot description: shown on the "Open bot" landing page above the
    # Start button. Telegram caps this at 512 chars; what's set in
    # BotFather is overwritten the next time we boot.
    bot_description = (
        "🔍 AutoSearch — лучший инструмент для пользователей торговых "
        "площадок. Моментально присылает все новые объявления.\n\n"
        "🎁 Бесплатный пробный период\n"
        "📋 До 5 одновременных поисков\n"
        "🛒 Огромный выбор площадок\n"
        "(Avito, Kufar, Olx, Vinted, Mercari и другие)\n"
        "💵 Лучшая цена на рынке. Одна находка позволяет полностью "
        "окупить подписку в несколько раз.\n\n"
        "👇 Нажми «Старт» чтобы начать 👇"
    )
    short_description = (
        "Мониторит Avito, OLX, Vinted, Kufar, Mercari — присылает новые "
        "объявления в реальном времени."
    )
    try:
        await bot.set_my_description(bot_description)
        await bot.set_my_short_description(short_description)
    except Exception as e:
        # Don't block startup if Telegram rejects the description (e.g.
        # rate-limited on rapid restarts). The previous value stays.
        logger.warning("set_my_description failed: %s", e)

    try:
        logger.info("Bot starting...")
        await dp.start_polling(
            bot,
            handle_signals=sys.platform == "win32",
            polling_timeout=10,
        )
    finally:
        logger.info("Shutting down...")
        stop_event.set()
        try:
            await asyncio.wait_for(scheduler_task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            scheduler_task.cancel()
        try:
            await bot.session.close()
        except Exception:
            pass
        await db.close()
        logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())
