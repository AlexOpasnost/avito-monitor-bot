"""Currency conversion + price rendering.

Approximate USD-pegged rates (April 2026). Used to render the same
listing's price in whichever currency the user picked at /start. The
estimate is a hint — accuracy beyond a few percent doesn't matter for
a marketplace monitor.

Rates can later move to a daily-updating source (ECB / open.er-api)
without changing call sites — `convert` and `format_price` are the
only public surface.
"""
from __future__ import annotations


# ISO-4217 → USD value of 1 unit. Add new currencies here.
USD_RATES: dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "JPY": 0.0067,
    "CNY": 0.14,
    "RUB": 0.011,
    "UAH": 0.024,
    "PLN": 0.25,
    "KZT": 0.0021,
    "BYN": 0.31,
    "RON": 0.22,
    "BGN": 0.57,
    "BRL": 0.20,
    "UZS": 0.000077,
    "BAM": 0.58,
    "CZK": 0.044,
    "HUF": 0.0028,
    "TRY": 0.029,
    "GEL": 0.37,
    "AMD": 0.0026,
    "AZN": 0.59,
    "ILS": 0.27,
    "HKD": 0.13,
    "KRW": 0.00075,
}

# ISO-4217 → human symbol for inline rendering. Falls back to the code
# itself for currencies we don't have a glyph for.
SYMBOLS: dict[str, str] = {
    "USD": "$",  "EUR": "€",  "GBP": "£",  "JPY": "¥",
    "CNY": "¥",  "RUB": "₽",  "UAH": "₴",  "PLN": "zł",
    "KZT": "₸",  "BYN": "Br", "RON": "lei", "BGN": "лв",
    "BRL": "R$", "UZS": "сум","BAM": "KM", "CZK": "Kč",
    "HUF": "Ft", "TRY": "₺",  "GEL": "₾",  "AMD": "֏",
    "AZN": "₼",  "ILS": "₪",  "HKD": "HK$", "KRW": "₩",
}


def convert(value: float | int, src: str, dst: str) -> float | None:
    """Convert `value` from `src` to `dst` via USD as pivot.

    Returns None when either currency is unknown. Same-currency call
    short-circuits to the input unchanged.
    """
    if value is None:
        return None
    if not src or not dst:
        return None
    s = src.strip().upper()
    d = dst.strip().upper()
    if s == d:
        return float(value)
    sr = USD_RATES.get(s)
    dr = USD_RATES.get(d)
    if not sr or not dr:
        return None
    usd = float(value) * sr
    return usd / dr


def symbol(code: str | None) -> str:
    if not code:
        return ""
    return SYMBOLS.get(code.strip().upper(), code.strip().upper())


def _format_amount(amount: float, currency: str) -> str:
    """Round + render an amount with thin spaces for thousands.

    Sub-USD currencies (JPY, KRW, etc.) get integer rendering since
    fractional yen is meaningless. Big-magnitude conversions (RUB, KZT)
    are rounded to the nearest 10 for a less-noisy hint.
    """
    cur = currency.upper()
    if cur in ("JPY", "KRW", "UZS"):
        n = int(round(amount))
    elif cur in ("RUB", "KZT", "HUF", "AMD"):
        # Round to nearest 10 for big numbers so "1297" doesn't look
        # like a real price quote — it's an estimate, after all.
        n = int(round(amount / 10.0)) * 10
    else:
        # 2-decimal precision for "regular" currencies; drop trailing
        # zeros so "14.00 €" renders as "14 €" but "14.50 €" stays.
        rounded = round(float(amount), 2)
        if abs(rounded - int(rounded)) < 0.005:
            n = int(rounded)
        else:
            return f"{rounded:.2f} {symbol(cur)}".replace(".", ",")
    # Thin-space thousands separator. Telegram renders these cleanly.
    return f"{n:,} {symbol(cur)}".replace(",", " ")


def format_native(value: float | int | None, currency: str | None,
                  fallback: str | None = None) -> str:
    """Render `value` in its native currency, or fall back to `fallback`.

    Used when a parser failed to extract a numeric value but still
    captured the seller's price string."""
    if value is None or not currency:
        return fallback or "—"
    return _format_amount(float(value), currency)


def format_with_estimate(
    value: float | int | None, src_currency: str | None,
    user_currency: str, fallback: str | None = None,
) -> str:
    """Render `<native price> (~<estimate in user's currency>)`.

    The user-currency estimate is dropped when:
      - source currency is missing or unknown
      - source == user (no conversion needed)
      - conversion fails (unknown user currency)
    """
    native = format_native(value, src_currency, fallback)
    if value is None or not src_currency:
        return native
    if src_currency.upper() == user_currency.upper():
        return native
    est = convert(value, src_currency, user_currency)
    if est is None or est <= 0:
        return native
    return f"{native} (~{_format_amount(est, user_currency)})"
