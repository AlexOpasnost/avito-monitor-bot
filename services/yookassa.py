"""YooKassa REST API client.

We use the REST API instead of the native Telegram-Payments path
because the native flow doesn't support СБП — it's locked to cards,
SberPay, and YooMoney. The REST path returns a `confirmation_url`
that YooKassa hosts; the user picks any enabled method on that
page (СБП, card, Apple Pay, …), then YooKassa POSTs us a webhook
to confirm the payment.

Auth: HTTP Basic with `shop_id:secret_key`. The secret key never
leaves the server — only the bot makes outbound calls and only the
webhook handler validates incoming events by re-fetching the
payment with these credentials.

Receipt requirements: самозанятые must register every fiscal
receipt through Мой налог (422-ФЗ); YooKassa handles the
registration on our behalf as long as we pass `receipt.items` in
the create-payment body. `vat_code=1` (без НДС) matches НПД rules.
"""
from __future__ import annotations

import logging
import re
import secrets
from decimal import Decimal, ROUND_HALF_UP

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.yookassa.ru/v3"
TIMEOUT_SECONDS = 15.0


class YooKassaError(Exception):
    """Raised when YooKassa returns a non-2xx or the request fails."""


def _sanitize_error_body(text: str | None, max_len: int = 200) -> str:
    """Trim YooKassa error bodies for safe logging.

    Errors from YooKassa sometimes echo customer-controlled data back
    (the email we passed, the description), and on `cancellation_details`
    rare paths can include card metadata. Logging the full body wrote
    that into Railway logs / any downstream sink. Strip emails and
    keep the structure ({code, parameter, type, description}) but
    mask values that look like personal data.
    """
    if not text:
        return ""
    sample = text[:max_len]
    # Mask anything that smells like an email — leaves enough to
    # identify the value class without exposing the address.
    sample = re.sub(
        r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}",
        "<email>", sample,
    )
    # Mask 13–19 digit runs (PAN-shaped). YooKassa shouldn't leak full
    # PANs, but cancellation_details has been observed with last4 in
    # the past — still mask just in case.
    sample = re.sub(r"\b\d{13,19}\b", "<digits>", sample)
    # Mask Russian phone numbers (+7/8 + 10 digits, with optional spaces,
    # dashes, or parentheses). YooKassa errors that complain about a
    # malformed phone in receipt.customer echo it back verbatim.
    sample = re.sub(
        r"(?:\+7|7|8)[\s\-()]*\d(?:[\s\-()]*\d){9}",
        "<phone>", sample,
    )
    # Mask SNILS (XXX-XXX-XXX YY) — should never appear here, but if a
    # caller ever wires it into description/metadata we don't want it
    # in plain logs.
    sample = re.sub(
        r"\b\d{3}[\s\-]\d{3}[\s\-]\d{3}[\s\-]\d{2}\b",
        "<snils>", sample,
    )
    return sample


async def create_payment(
    *, shop_id: str, secret_key: str,
    amount_rub: float, description: str,
    metadata: dict, return_url: str,
    receipt_items: list[dict] | None = None,
    customer_email: str | None = None,
    customer_phone: str | None = None,
) -> dict:
    """Create a YooKassa payment and return the response dict.

    The returned object includes `confirmation.confirmation_url` —
    this is the URL we send the user as a button. After they pay,
    YooKassa redirects them back to `return_url` (we use the bot's
    Telegram deep-link so they land back in the chat) AND POSTs us
    a webhook on the configured endpoint.

    `metadata` is stored alongside the payment and echoed back in
    the webhook event. We put `telegram_id` and `tariff_id` here so
    the webhook handler can route activation without holding any
    server-side state between create and confirm.

    When the shop has Мой налог auto-receipts on (mandatory for
    самозанятый), YooKassa requires `receipt.customer.email` or
    `.phone` at create time — passing the receipt block alone
    fails with HTTP 400. The hosted page does NOT collect this on
    our behalf, contrary to what the docs imply. Caller must pass
    `customer_email` (or `customer_phone`).
    """
    # Format the amount through Decimal to dodge IEEE-754 rounding.
    # f"{0.1+0.2:.2f}" happens to print 0.30, but for any amount
    # whose binary repr can't be exactly halved (kopeks/100.0 of a
    # fractional ruble), the float→string conversion can produce
    # 99999.98 vs the expected 99999.99 — YooKassa rejects with
    # "amount.value invalid". Decimal(str(...)) then quantize is the
    # asyncpg-recommended pattern for currency strings.
    amount_str = format(
        Decimal(str(amount_rub)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        "f",
    )
    body = {
        "amount": {
            "value": amount_str,
            "currency": "RUB",
        },
        # Auto-capture: charge immediately, no two-stage hold.
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": return_url,
        },
        "description": description[:128],
        "metadata": metadata,
    }
    if receipt_items:
        receipt: dict = {"items": receipt_items}
        customer: dict = {}
        if customer_email:
            customer["email"] = customer_email
        if customer_phone:
            customer["phone"] = customer_phone
        if customer:
            receipt["customer"] = customer
        body["receipt"] = receipt

    # YooKassa requires Idempotence-Key on POSTs that mutate state.
    # Passing a fresh random key on each create is correct — if our
    # request times out, the retry will send a different key and
    # YooKassa creates a new payment. (We never silently retry
    # client-side, so this is fine.)
    idempotency_key = secrets.token_hex(16)

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{API_BASE}/payments",
                auth=(shop_id, secret_key),
                json=body,
                headers={"Idempotence-Key": idempotency_key},
            )
    except httpx.RequestError as e:
        logger.exception("[yookassa] create_payment network err: %s", e)
        raise YooKassaError(f"network: {e}") from e

    if resp.status_code >= 400:
        sanitized = _sanitize_error_body(resp.text)
        logger.error(
            "[yookassa] create_payment %d body=%s",
            resp.status_code, sanitized,
        )
        raise YooKassaError(
            f"create_payment HTTP {resp.status_code}: {sanitized}"
        )
    return resp.json()


async def get_payment(
    payment_id: str, *, shop_id: str, secret_key: str,
) -> dict:
    """Fetch an existing payment by id.

    Used by the webhook handler as a verification step: a forged
    POST to /webhook/yookassa can carry any payment_id, but only
    a real payment can be retrieved with our shop credentials.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{API_BASE}/payments/{payment_id}",
                auth=(shop_id, secret_key),
            )
    except httpx.RequestError as e:
        logger.exception("[yookassa] get_payment network err: %s", e)
        raise YooKassaError(f"network: {e}") from e

    if resp.status_code >= 400:
        sanitized = _sanitize_error_body(resp.text)
        logger.error(
            "[yookassa] get_payment %d body=%s",
            resp.status_code, sanitized,
        )
        raise YooKassaError(
            f"get_payment HTTP {resp.status_code}: {sanitized}"
        )
    return resp.json()


async def get_refund(
    refund_id: str, *, shop_id: str, secret_key: str,
) -> dict:
    """Fetch an existing refund by id.

    Used by the refund webhook handler as a verification step. The
    POST body of a refund.succeeded event is fully attacker-controlled
    if the IP allowlist is ever bypassed (X-Forwarded-For spoofing,
    misconfigured proxy, direct path) — without this GET-back, an
    attacker could deactivate any user's tariff and spam them with a
    "refund processed" DM. With it, only refunds that actually exist
    in our shop's books can drive deactivation.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{API_BASE}/refunds/{refund_id}",
                auth=(shop_id, secret_key),
            )
    except httpx.RequestError as e:
        logger.exception("[yookassa] get_refund network err: %s", e)
        raise YooKassaError(f"network: {e}") from e

    if resp.status_code >= 400:
        sanitized = _sanitize_error_body(resp.text)
        logger.error(
            "[yookassa] get_refund %d body=%s",
            resp.status_code, sanitized,
        )
        raise YooKassaError(
            f"get_refund HTTP {resp.status_code}: {sanitized}"
        )
    return resp.json()


def build_receipt_item(
    name: str, amount_rub: float, *,
    vat_code: int = 1,
    payment_subject: str = "service",
) -> dict:
    """Build a single receipt line item for a tariff purchase.

    Defaults match the самозанятый (НПД) tax regime:
      - vat_code=1 → «Без НДС»
      - payment_mode='full_payment' → customer pays in full at receipt time
      - payment_subject='service' → digital subscription is closest to FFD
        1.2's «service» category (no «subscription» enum value exists)
    """
    # Same Decimal-formatting reasoning as create_payment. The receipt
    # amount in Мой налог is the legally binding fiscal record — even
    # a 1-kopek mismatch with create_payment.amount triggers ОФД
    # rejection.
    amount_str = format(
        Decimal(str(amount_rub)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        "f",
    )
    return {
        "description": name[:128],
        "quantity": "1.00",
        "amount": {
            "value": amount_str,
            "currency": "RUB",
        },
        "vat_code": vat_code,
        "payment_mode": "full_payment",
        "payment_subject": payment_subject,
    }
