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

import ipaddress
import logging

from aiogram import Bot
from aiohttp import web

from config import config
from database import db, UserTombstonedError
from services.yookassa import YooKassaError, get_payment, get_refund

logger = logging.getLogger(__name__)


def _client_ip(request: web.Request) -> str | None:
    """Resolve the real client IP behind Railway's edge proxy.

    Security note: the **leftmost** entry of `X-Forwarded-For` is fully
    attacker-controlled — any HTTP client can set
    `X-Forwarded-For: 185.71.76.5, real-attacker-ip` and the leftmost
    value will look like a YooKassa CIDR even though the connection
    came from somewhere else. The previous code did exactly that and
    was bypassable by anyone who could reach the aiohttp listener
    directly (Railway internal network co-tenants, future re-deploys
    without an edge proxy, or local dev).

    Correct trust model: trust the **rightmost** hop that *we*
    appended (the one closest to our server). Railway's edge sets
    `X-Real-IP` to the canonical client address after stripping the
    untrusted leftmost entries, so prefer that when present. Fall back
    to the rightmost X-Forwarded-For entry, then to the peer remote.
    """
    real_ip = request.headers.get("X-Real-IP", "").strip()
    if real_ip:
        return real_ip
    fwd = request.headers.get("X-Forwarded-For", "").strip()
    if fwd:
        # Rightmost = the IP the upstream proxy *we* trust observed.
        # Leftmost is whatever the client typed and is forgeable.
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.remote


def _is_yookassa_ip(ip_str: str | None) -> bool:
    """Match `ip_str` against the configured YooKassa CIDR allowlist.

    Fails CLOSED on an empty allowlist: an operator who clears
    YOOKASSA_ALLOWED_IPS by accident (typo, sed mistake, blank value
    pushed by mistake) gets every webhook 403'd until they fix it,
    which is loud and obvious. The previous behaviour was fail-OPEN —
    empty list returned True and silently disabled the entire IP
    gate. The GET-back verification with our shop secret is still in
    place as the second line of defence, but the IP gate must remain
    a hard gate: if it isn't actually filtering, we'd want to know.
    """
    allow = config.yookassa_allowed_ips or []
    if not allow:
        logger.error(
            "[yookassa-wh] YOOKASSA_ALLOWED_IPS is empty — failing closed. "
            "Set the env var to YooKassa's published IP list."
        )
        return False
    if not ip_str:
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for cidr in allow:
        try:
            if ip in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


async def handle_yookassa_webhook(request: web.Request) -> web.Response:
    # IP gate — refuse non-YooKassa sources outright. Defense in
    # depth: a forged POST with the right shape would still fail
    # the GET-back check below, but the IP gate stops the attack
    # at the door so we don't burn YooKassa API quota on every
    # random scanner that finds the endpoint.
    client_ip = _client_ip(request)
    if not _is_yookassa_ip(client_ip):
        logger.warning(
            "[yookassa-wh] reject non-allowlisted IP=%s", client_ip,
        )
        return web.Response(status=403, text="forbidden")

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

    # Refund delivery — when the operator processes a return through
    # YooKassa dashboard, the merchant gets a separate refund.succeeded
    # event. We revoke the user's tariff so they don't keep getting
    # paid features after their money is gone. Idempotent: a tariff
    # that's already NULL stays NULL.
    if event == "refund.succeeded":
        return await _handle_refund_event(request, obj)

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

    # 152-ФЗ Art. 14 erasure honoring: if the user invoked
    # /delete_my_account at any point, never recreate their row.
    # The late YooKassa webhook still needs to be acknowledged (we
    # can't refund automatically — that's an operator action), but
    # we MUST NOT resurrect the deleted user's PII.
    if await db.is_telegram_id_tombstoned(telegram_id):
        logger.error(
            "[yookassa-wh] payment %s for tombstoned telegram_id=%d — "
            "refusing to recreate user; manual reconciliation required",
            payment_id, telegram_id,
        )
        # Ack 200 so YooKassa stops retrying; revenue is on the books
        # via the (un-recorded here) charge — operator must refund
        # manually via YooKassa dashboard.
        return web.Response(status=200, text="ok")

    # The pre-check above is a fast rejection. The TOCTOU window between
    # that read and the get_or_create_user / record_and_activate path
    # below is closed by the advisory lock inside both helpers — but if
    # /delete_my_account commits during this window and we still try
    # to recreate, get_or_create_user raises UserTombstonedError. Catch
    # it the same way as the pre-check: 200 + manual reconciliation.
    try:
        user_id = await db.get_or_create_user(telegram_id, None)
    except UserTombstonedError:
        logger.error(
            "[yookassa-wh] payment %s — telegram_id=%d tombstoned mid-tx; "
            "refusing to recreate user; manual reconciliation required",
            payment_id, telegram_id,
        )
        return web.Response(status=200, text="ok")

    amount = payment.get("amount") or {}
    currency = (amount.get("currency") or "RUB").upper()
    try:
        amount_minor = int(round(float(amount.get("value", "0")) * 100))
    except (TypeError, ValueError):
        amount_minor = 0

    # Atomic record + activate. The combined transaction prevents the
    # "paid but no tariff" failure mode that the old two-step flow had:
    # if record_payment succeeded server-side but the client never got
    # the ack, _execute's retry-on-network-error would re-run the INSERT,
    # hit ON CONFLICT, and tell us "already activated" — but the second
    # step (activate_tariff) had never actually run. Customer money,
    # no service. See database.record_and_activate_payment for the full
    # rationale.
    try:
        is_new, new_exp = await db.record_and_activate_payment(
            telegram_charge_id=payment_id,
            provider_charge_id=payment_id,
            user_id=user_id,
            tariff_id=tariff_id,
            amount_minor=amount_minor,
            currency=currency,
            hours=rules["hours"],
        )
    except Exception:
        logger.exception(
            "[yookassa-wh] record_and_activate failed: payment=%s user=%d tariff=%s",
            payment_id, user_id, tariff_id,
        )
        # Return 500 so YooKassa retries — nothing committed because the
        # whole record+activate is a single transaction. A retry will
        # either succeed cleanly or hit the same error for triage.
        return web.Response(status=500, text="retry")

    # Sentinel: user row vanished between get_or_create_user and the
    # FOR UPDATE inside record_and_activate (race with delete). NO
    # payment was recorded — the transaction returned None,None before
    # the INSERT. Ask YooKassa to retry; if the user re-registers in
    # the meantime, the retry succeeds. If they never come back,
    # operator must reconcile via YooKassa refund dashboard.
    if is_new is None:
        logger.error(
            "[yookassa-wh] payment %s — user_id=%d vanished mid-tx; "
            "returning 500 so YooKassa retries",
            payment_id, user_id,
        )
        return web.Response(status=500, text="retry")

    if not is_new:
        logger.info("[yookassa-wh] duplicate %s — already activated", payment_id)
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


async def _handle_refund_event(request: web.Request, refund_obj: dict) -> web.Response:
    """Refund cycle — the operator hit «Возврат» in YooKassa dashboard
    and the customer's card got the money back. We deactivate the
    tariff so the user doesn't keep getting paid features for free.

    Two defenses against forged/replayed refund POSTs:

    1. GET-back to /v3/refunds/{id} with our shop credentials. A
       forged refund event with an arbitrary `payment_id` would let
       an attacker deactivate any user's tariff (and spam their DM)
       once the IP allowlist is bypassed. Looking the refund up by
       its own id, with our secret, means only refunds that actually
       exist on YooKassa's books can drive deactivation here. We
       also read the canonical `payment_id` from this response, not
       from the POST body.

    2. `refunds` idempotency table. A second delivery of the same
       refund_id (legitimate retry, replay, our own _execute retry)
       is a no-op — no second deactivation, no second DM. Without
       this, an attacker spraying duplicates triggers Telegram's
       anti-spam against the bot.
    """
    refund_id = refund_obj.get("id")
    if not refund_id:
        logger.warning("[yookassa-wh] refund event missing refund id")
        return web.Response(status=200, text="ok")

    if not (config.yookassa_shop_id and config.yookassa_secret_key):
        logger.error("[yookassa-wh] YOOKASSA_SHOP_ID/SECRET_KEY not configured")
        return web.Response(status=503, text="not configured")

    # Re-fetch the refund from YooKassa with our shop credentials.
    # Forged POSTs can carry any refund id; only real refunds in our
    # books come back with a 200 here.
    try:
        refund = await get_refund(
            refund_id,
            shop_id=config.yookassa_shop_id,
            secret_key=config.yookassa_secret_key,
        )
    except YooKassaError as e:
        logger.error("[yookassa-wh] refund verify failed for %s: %s", refund_id, e)
        # Ack 200 — same reasoning as the payment.succeeded path.
        return web.Response(status=200, text="ok")

    status = refund.get("status")
    if status != "succeeded":
        logger.info(
            "[yookassa-wh] refund %s status=%s — skipping deactivation",
            refund_id, status,
        )
        return web.Response(status=200, text="ok")

    # Authoritative payment_id from the verified refund response, not
    # the POST body.
    payment_id = refund.get("payment_id")
    if not payment_id:
        logger.warning(
            "[yookassa-wh] refund %s has no payment_id in verified response",
            refund_id,
        )
        return web.Response(status=200, text="ok")

    user_id = await db.get_payment_user_id(payment_id)
    if user_id is None:
        logger.warning(
            "[yookassa-wh] refund %s for unknown payment_id=%s — no-op",
            refund_id, payment_id,
        )
        return web.Response(status=200, text="ok")

    amount = refund.get("amount") or {}
    currency = (amount.get("currency") or "RUB").upper()
    try:
        amount_minor = int(round(float(amount.get("value", "0")) * 100))
    except (TypeError, ValueError):
        amount_minor = 0

    # Atomic record + deactivate. The previous split-step flow left
    # a window where the refund was logged but the tariff stayed
    # active — if `deactivate_user_tariff` failed transiently, the
    # next webhook delivery saw is_new=False and the deactivation
    # never re-ran. See database.record_and_deactivate_refund.
    try:
        is_new = await db.record_and_deactivate_refund(
            refund_id=refund_id,
            payment_id=payment_id,
            user_id=user_id,
            amount_minor=amount_minor,
            currency=currency,
        )
    except Exception:
        logger.exception(
            "[yookassa-wh] record_and_deactivate_refund failed: "
            "refund=%s payment=%s user=%d",
            refund_id, payment_id, user_id,
        )
        # 500 → YooKassa retries; the txn rolled back, so the refund
        # row is NOT recorded yet — retry will hit the same INSERT
        # path cleanly (or fail again for the same reason, which is
        # the operator's signal to look at the logs).
        return web.Response(status=500, text="retry")

    if not is_new:
        logger.info("[yookassa-wh] duplicate refund %s — already processed", refund_id)
        return web.Response(status=200, text="ok")

    logger.info(
        "[yookassa-wh] refund %s — deactivated tariff for user=%d (payment=%s)",
        refund_id, user_id, payment_id,
    )

    # Best-effort DM to the user — only sent on the first (is_new=True)
    # processing of this refund_id, so a duplicate delivery never spams.
    bot: Bot = request.app["bot"]
    try:
        tg_id = await db.get_telegram_id(user_id)
        if tg_id:
            await bot.send_message(
                tg_id,
                "💸 Возврат прошёл. Тариф отключён, текущие поиски остаются "
                "сохранёнными — активируешь их, когда снова оформишь подписку.",
            )
    except Exception as e:
        logger.warning("[yookassa-wh] refund notify failed: %s", e)

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
