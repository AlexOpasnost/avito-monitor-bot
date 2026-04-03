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
from scheduler import run_scheduler
from parser import rotate_ip, check_proxy_ip

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
sys.stdout.reconfigure(line_buffering=True)
logger = logging.getLogger(__name__)


async def main():
    if not config.bot_token:
        logger.error("BOT_TOKEN not set in .env file")
        sys.exit(1)

    # Connect to database
    await db.connect()
    logger.info("Database connected")

    # Verify proxy and rotate IP at startup
    if config.proxy_list:
        logger.info("Proxy configured: %s", config.proxy_list[0].split("@")[-1])
        await rotate_ip()
        ip = await check_proxy_ip()
        if ip:
            logger.info("Proxy working! External IP: %s", ip)
        else:
            logger.error("Proxy NOT working — requests will fail!")
    else:
        logger.warning("No proxy configured — Avito will likely block requests")

    # Use custom Telegram API URL if set (for local dev behind firewall)
    if config.telegram_api_url and config.telegram_api_url != "https://api.telegram.org":
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(config.telegram_api_url)
        )
        bot = Bot(token=config.bot_token, session=session)
        logger.info("Using custom Telegram API: %s", config.telegram_api_url)
    else:
        bot = Bot(token=config.bot_token)
        logger.info("Using direct Telegram API")
    dp = Dispatcher()
    dp.include_router(router)

    # Graceful shutdown event
    stop_event = asyncio.Event()

    # Start scheduler as background task
    scheduler_task = asyncio.create_task(run_scheduler(bot, stop_event))

    # Handle shutdown signals
    def shutdown_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, shutdown_handler)

    # Register bot commands (menu button)
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="profile", description="Мой профиль и статистика"),
        BotCommand(command="list", description="Активные отслеживания"),
        BotCommand(command="delete", description="Удалить отслеживание"),
        BotCommand(command="stop", description="Остановить всё"),
    ])

    try:
        logger.info("Bot starting...")
        await dp.start_polling(
            bot,
            handle_signals=sys.platform == "win32",
            polling_timeout=10,  # Very short for CF Worker (30s limit)
        )
    finally:
        logger.info("Shutting down...")
        stop_event.set()

        # Wait for scheduler to finish
        try:
            await asyncio.wait_for(scheduler_task, timeout=10)
        except asyncio.TimeoutError:
            scheduler_task.cancel()

        await bot.session.close()
        await db.close()
        logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())
