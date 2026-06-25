"""Avito Monitor Bot — main entry point."""
import asyncio
import logging
import os
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import BotCommand
from aiohttp import web

from config import config
from database import db
from handlers import router
from middleware import PerUserThrottle
from parser import check_proxy_ip, rotate_ip
from scheduler import run_scheduler
from webhook import build_app as build_webhook_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
# httpx logs every outbound request at INFO with the FULL URL. That
# leaks the mobileproxy `proxy_key` (we redact it in our own
# parsers.common logs, but httpx's internal logger bypasses that
# entirely). Pin httpx + httpcore to WARNING — request errors still
# surface, but the per-request URL line goes away.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass
logger = logging.getLogger(__name__)


def _init_sentry() -> None:
    """Wire Sentry crash + error reporting if SENTRY_DSN is set.

    Free tier (5k events/mo) is enough for low-volume bots. Without
    this, exceptions land in Railway logs only — when a customer
    reports a problem, the operator has to grep by timestamp. With
    Sentry, the operator gets a Telegram/email alert seconds after
    the failure with the full traceback and request context.

    `traces_sample_rate=0` keeps performance traces off (errors only,
    which is the highest-value low-cost signal). The kwargs avoid
    asyncio CancelledError + aiogram retry-after noise.
    """
    if not config.sentry_dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        from sentry_sdk.integrations.aiohttp import AioHttpIntegration
    except ImportError:
        logger.warning(
            "[sentry] SENTRY_DSN set but sentry-sdk not installed — "
            "skipping Sentry init. Add sentry-sdk to requirements.txt."
        )
        return

    # PII scrubbing for outbound events. Sentry's HTTP integrations
    # capture request/response breadcrumbs at the transport layer,
    # BEFORE our application-level _sanitize_error_body runs. Without
    # the scrubber below, every YooKassa POST whose body contains
    # `metadata.telegram_id` or `receipt.customer.email`, every
    # outbound Telegram API call carrying chat IDs and message text,
    # and every httpx Authorization header (Basic shop_id:secret_key)
    # lands in Sentry events verbatim. This is a 152-ФЗ §6 violation
    # waiting to happen.
    def _scrub_event(event, hint):
        # Drop request bodies entirely — we never need them for
        # debugging, and they almost always contain PII (Telegram
        # message text, YooKassa metadata, customer email).
        request = event.get("request") or {}
        if "data" in request:
            request["data"] = "[scrubbed]"
        if "headers" in request:
            headers = request["headers"] or {}
            for k in list(headers):
                if k.lower() in ("authorization", "x-yookassa-signature",
                                 "cookie", "set-cookie"):
                    headers[k] = "[scrubbed]"
            request["headers"] = headers
        # Same scrub on breadcrumbs — httpx integration emits one
        # breadcrumb per request and pre-fills `data` with the body
        # for non-2xx responses.
        for crumb in event.get("breadcrumbs", {}).get("values", []) or []:
            if crumb.get("category") in ("httplib", "httpx", "aiohttp"):
                data = crumb.get("data") or {}
                for k in ("body", "request_body", "response_body",
                          "Authorization"):
                    if k in data:
                        data[k] = "[scrubbed]"
                crumb["data"] = data
        return event

    sentry_sdk.init(
        dsn=config.sentry_dsn,
        environment=config.sentry_environment,
        traces_sample_rate=0.0,
        integrations=[AsyncioIntegration(), AioHttpIntegration()],
        # Default in 2.x is False, but be explicit — flipping the
        # default by accident in a future SDK upgrade would silently
        # start exfiltrating PII.
        send_default_pii=False,
        before_send=_scrub_event,
        before_breadcrumb=lambda crumb, hint: crumb,
        # Tag the deploy so rollbacks can correlate to error spikes.
        # Railway injects RAILWAY_GIT_COMMIT_SHA; falls back to "dev"
        # locally, which is fine — Sentry shows it in the UI.
        release=os.getenv("RAILWAY_GIT_COMMIT_SHA", "dev"),
        # Drop non-actionable noise. Use the class itself, not the
        # string name — string matching missed concurrent.futures.
        ignore_errors=[asyncio.CancelledError],
    )
    logger.info("[sentry] enabled (env=%s)", config.sentry_environment)


_init_sentry()


async def main():
    if not config.bot_token:
        logger.error("BOT_TOKEN not set in .env")
        sys.exit(1)

    await db.connect()
    logger.info("Database connected")

    if config.proxy_list:
        # Use urlparse-based redaction — `.split("@")[-1]` was fragile:
        # it returns the full string for proxies without userinfo
        # (socks5://1.2.3.4:1080 → "socks5://1.2.3.4:1080") and would
        # mis-handle passwords containing '@'. _redact_proxy_for_log
        # strips userinfo properly via urllib.parse.
        from parsers.common import _redact_proxy_for_log
        logger.info("Proxy configured: %s", _redact_proxy_for_log(config.proxy_list[0]))
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
    # Per-user throttle: drop events faster than 2/sec for messages,
    # 3/sec for callbacks. Without this, a single user can spam /list
    # × 1000 and stall the asyncpg pool (max_size=5) for every other
    # user. Callback rate is a bit higher because real navigation
    # (rapid menu clicks) legitimately fires several events per second.
    dp.message.middleware(PerUserThrottle(rate_seconds=0.5, label="msg"))
    # edited_message bypasses dp.message — without its own throttle a
    # user can hold down the up-arrow to re-edit and replay each command
    # at line rate. Same budget as fresh messages.
    dp.edited_message.middleware(PerUserThrottle(rate_seconds=0.5, label="edit"))
    dp.callback_query.middleware(PerUserThrottle(rate_seconds=0.3, label="cb"))
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

    # NOTE: bot.set_my_description / set_my_short_description disabled.
    #
    # As of ROUND 14 the call started failing every boot with
    # `BOT_SHARETEXT_INVALID` (Telegram anti-spam rejecting the new
    # multi-line description). On top of being noise in the log,
    # repeatedly calling setMyDescription with a rejected payload
    # appears to put the bot into a soft-ban state where outgoing
    # `sendMessage` returns 200 OK to us but Telegram silently does
    # not deliver the message to users — we observed this exact
    # symptom in production (handlers run, no errors, users see
    # nothing). Keep description set manually via @BotFather instead
    # until we figure out which character/emoji triggers the
    # rejection. Operator can flip this back on with a config flag
    # later.

    # Webhook server runs alongside polling on the same event loop.
    # Railway injects $PORT for the public-facing service — honour it.
    webhook_runner: web.AppRunner | None = None
    try:
        webhook_app = build_webhook_app(bot)
        webhook_runner = web.AppRunner(webhook_app)
        await webhook_runner.setup()
        port = int(os.getenv("PORT", str(config.webhook_port)))
        site = web.TCPSite(webhook_runner, "0.0.0.0", port)
        await site.start()
        logger.info("Webhook server listening on :%d (POST /webhook/yookassa)", port)
    except Exception:
        # Webhook bring-up failures shouldn't block polling — paid
        # tariffs simply won't activate until restart, but the bot
        # itself stays usable for free / admin / legacy users.
        logger.exception("Webhook server failed to start — continuing without it")
        webhook_runner = None

    try:
        logger.info("Bot starting...")
        # Drop any stale webhook from previous configurations (e.g. an
        # old Cloudflare worker) AND drop any pending updates that
        # accumulated while the previous container was dying. Without
        # this, a webhook URL left behind makes getUpdates 409 forever
        # ("terminated by other getUpdates request"), and the boot
        # loop fights itself for ~30 s on every redeploy.
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("Webhook cleared, polling start")
        except Exception:
            logger.warning("delete_webhook on boot failed — continuing", exc_info=True)
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
            # Re-await after cancel so the task actually drains its
            # cancellation and releases the asyncpg / aiohttp resources
            # it owns. Without this the task stays in CANCELLING and
            # leaks the connection until process exit.
            try:
                await scheduler_task
            except (asyncio.CancelledError, Exception):
                pass
        if webhook_runner is not None:
            try:
                await webhook_runner.cleanup()
            except Exception:
                pass
        try:
            await bot.session.close()
        except Exception:
            pass
        # Drain cached cloudscraper sessions so their urllib3 pools
        # release sockets cleanly (prevents ResourceWarning spam on
        # local dev shutdown and helps if Railway gives us a graceful
        # SIGTERM window before SIGKILL).
        try:
            from parsers.common import close_all_sessions
            close_all_sessions()
        except Exception:
            logger.debug("[shutdown] close_all_sessions raised", exc_info=True)
        await db.close()
        logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())
