"""YooKassa webhook server.

Endpoint: POST /webhook/yookassa
Health:   GET  /health (Railway uses this to know the service is up)

YooKassa POSTs us an event blob containing `event` (e.g.
`payment.succeeded`) and `object` (the payment row). We don't trust
the body — anyone could forge a POST to our public URL — so the
first thing we do is GET /v3/payments/{id} with our shop secret.
If YooKassa returns the payment with `status='succeeded'`, the
event is real and we activate the tariff.

Idempotency is reused from the previous Telegram-Payments path:
the `payments` table has `telegram_charge_id` as primary key, so
a duplicate webhook delivery just hits ON CONFLICT DO NOTHING and
no second activation happens.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiohttp import web

from config import config
from database import db
from services.yookassa import YooKassaError, get_payment

logger = logging.getLogger(__name__)


async def handle_yookassa_webhook(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        logger.warning("[yookassa-wh] non-json body")
        return web.Response(status=400, text="bad json")

    event = (body.get("event") or "").lower()
    obj = body.get("object") or {}
    payment_id = obj.get("id")
    if not payment_id:
        logger.warning("[yookassa-wh] no payment id in body=%s", str(body)[:200])
        return web.Response(status=400, text="no payment id")

    logger.info("[yookassa-wh] event=%s payment_id=%s", event, payment_id)

    # We only care about the "succeeded" terminal state. Other events
    # (waiting_for_capture, canceled) are acked + ignored so YooKassa
    # doesn't keep retrying.
    if event != "payment.succeeded":
        return web.Response(status=200, text="ok")

    # Verification step — calling GET with our secret confirms the
    # webhook came from YooKassa (forged POSTs can't impersonate
    # since they'd need our shop_id + secret_key to authenticate).
    if not (config.yookassa_shop_id and config.yookassa_secret_key):
        logger.error("[yookassa-wh] YOOKASSA_SHOP_ID/SECRET_KEY not configured")
        return web.Response(status=503, text="not configured")

    try:
        payment = await get_payment(
            payment_id,
            shop_id=config.yookassa_shop_id,
            secret_key=config.yookassa_secret_key,
        )
    except YooKassaError as e:
        logger.error("[yookassa-wh] verify failed for %s: %s", payment_id, e)
        # Ack with 200 so YooKassa stops retrying — we logged the
        # failure for manual triage. A 500 here would have YooKassa
        # hammer us indefinitely.
        return web.Response(status=200, text="ok")

    status = payment.get("status")
    if status != "succeeded":
        logger.info(
            "[yookassa-wh] payment %s status=%s — skipping activation",
            payment_id, status,
        )
        return web.Response(status=200, text="ok")

    metadata = payment.get("metadata") or {}
    raw_tg = metadata.get("telegram_id")
    tariff_id = metadata.get("tariff_id")
    if not raw_tg or not tariff_id:
        logger.warning(
            "[yookassa-wh] missing metadata: telegram_id=%r tariff_id=%r",
            raw_tg, tariff_id,
        )
        return web.Response(status=200, text="ok")
    try:
        telegram_id = int(raw_tg)
    except (TypeError, ValueError):
        logger.warning("[yookassa-wh] non-int telegram_id: %r", raw_tg)
        return web.Response(status=200, text="ok")

    # Local import — avoids a circular dep with handlers.py at module
    # load time (handlers imports from a lot of places, including
    # eventually scheduler which imports config which imports webhook
    # if we hoist it up).
    from handlers import _TARIFF_RULES, _tariff_meta

    rules = _TARIFF_RULES.get(tariff_id)
    if rules is None or tariff_id in ("legacy", "admin", "trial"):
        logger.warning(
            "[yookassa-wh] non-purchasable tariff in metadata: %s", tariff_id,
        )
        return web.Response(status=200, text="ok")

    user_id = await db.get_or_create_user(telegram_id, None)

    amount = payment.get("amount") or {}
    currency = (amount.get("currency") or "RUB").upper()
    try:
        amount_minor = int(round(float(amount.get("value", "0")) * 100))
    except (TypeError, ValueError):
        amount_minor = 0

    # Idempotency: payment_id is unique per YooKassa transaction.
    # ON CONFLICT DO NOTHING in record_payment makes the second
    # delivery a no-op.
    try:
        is_new = await db.record_payment(
            telegram_charge_id=payment_id,
            provider_charge_id=payment_id,
            user_id=user_id,
            tariff_id=tariff_id,
            amount_minor=amount_minor,
            currency=currency,
        )
    except Exception:
        logger.exception(
            "[yookassa-wh] record_payment failed: payment=%s user=%d tariff=%s",
            payment_id, user_id, tariff_id,
        )
        return web.Response(status=200, text="ok")

    if not is_new:
        logger.info("[yookassa-wh] duplicate %s — already activated", payment_id)
        return web.Response(status=200, text="ok")

    try:
        new_exp = await db.activate_tariff(
            user_id, tariff_id, rules["hours"], is_trial=False,
        )
    except Exception:
        logger.exception(
            "[yookassa-wh] activate_tariff failed AFTER record_payment: "
            "payment=%s user=%d tariff=%s",
            payment_id, user_id, tariff_id,
        )
        # Still ack — payments row already locked the charge_id, so
        # a retry won't double-activate; manual fix needed.
        return web.Response(status=200, text="ok")

    bot: Bot = request.app["bot"]
    meta = _tariff_meta(tariff_id)
    name = meta[1] if meta else tariff_id
    expires_str = new_exp.strftime("%d.%m.%Y %H:%M UTC")
    try:
        await bot.send_message(
            telegram_id,
            f"✅ <b>Оплата получена!</b>\n\n"
            f"Тариф <b>{name}</b> активирован до <b>{expires_str}</b>.\n"
            f"Можно добавлять поиски — открой главное меню /start.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(
            "[yookassa-wh] send_message to %d failed: %s",
            telegram_id, e,
        )

    logger.info(
        "[yookassa-wh] activated user=%d tariff=%s exp=%s amount=%d/%s",
        user_id, tariff_id, expires_str, amount_minor, currency,
    )
    return web.Response(status=200, text="ok")


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(status=200, text="ok")


def build_app(bot: Bot) -> web.Application:
    """Wire the aiohttp app. We attach the Bot instance to app state
    so the webhook handler can reach it without a global."""
    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/webhook/yookassa", handle_yookassa_webhook)
    app.router.add_get("/health", handle_health)
    return app
