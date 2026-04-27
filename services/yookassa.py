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
import secrets

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.yookassa.ru/v3"
TIMEOUT_SECONDS = 15.0


class YooKassaError(Exception):
    """Raised when YooKassa returns a non-2xx or the request fails."""


async def create_payment(
    *, shop_id: str, secret_key: str,
    amount_rub: float, description: str,
    metadata: dict, return_url: str,
    receipt_items: list[dict] | None = None,
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
    """
    body = {
        "amount": {
            "value": f"{amount_rub:.2f}",
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
        # `customer` is omitted on purpose — YooKassa's hosted page
        # collects the email/phone from the payer when the receipt
        # is required, so we don't have to.
        body["receipt"] = {"items": receipt_items}

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
        logger.error(
            "[yookassa] create_payment %d body=%s",
            resp.status_code, resp.text[:500],
        )
        raise YooKassaError(
            f"create_payment HTTP {resp.status_code}: {resp.text[:200]}"
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
        logger.error(
            "[yookassa] get_payment %d body=%s",
            resp.status_code, resp.text[:500],
        )
        raise YooKassaError(
            f"get_payment HTTP {resp.status_code}: {resp.text[:200]}"
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
    return {
        "description": name[:128],
        "quantity": "1.00",
        "amount": {
            "value": f"{amount_rub:.2f}",
            "currency": "RUB",
        },
        "vat_code": vat_code,
        "payment_mode": "full_payment",
        "payment_subject": payment_subject,
    }
