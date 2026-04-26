"""Bot UI internationalisation: language + currency picker data.

This module is *only* the registry of supported user-facing locales —
the actual translation of listing text happens in scheduler.py through
deep-translator. Listings get translated to whatever ISO-639-1 code the
user picked here.

Adding a language: append to LANGUAGES with the deep-translator code.
Adding a currency: append to CURRENCIES, and make sure the ISO-4217
code is also in parsers.currency.USD_RATES (otherwise conversion no-ops
and the user sees the listing's native price unchanged).
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# (code, label). `code` is the deep-translator/Google-Translate code.
# Order = display order in the picker.
LANGUAGES: list[tuple[str, str]] = [
    # CIS — first row: most users land here
    ("ru", "🇷🇺 Русский"),
    ("be", "🇧🇾 Беларуская"),
    ("uk", "🇺🇦 Українська"),
    ("kk", "🇰🇿 Қазақша"),
    # Major European
    ("en", "🇬🇧 English"),
    ("es", "🇪🇸 Español"),
    ("de", "🇩🇪 Deutsch"),
    ("fr", "🇫🇷 Français"),
    ("it", "🇮🇹 Italiano"),
    ("pl", "🇵🇱 Polski"),
    ("pt", "🇵🇹 Português"),
    ("nl", "🇳🇱 Nederlands"),
    ("tr", "🇹🇷 Türkçe"),
    ("ro", "🇷🇴 Română"),
    ("cs", "🇨🇿 Čeština"),
    ("hu", "🇭🇺 Magyar"),
    ("el", "🇬🇷 Ελληνικά"),
    ("bg", "🇧🇬 Български"),
    ("sv", "🇸🇪 Svenska"),
]

LANGUAGE_CODES = frozenset(c for c, _ in LANGUAGES)


def language_label(code: str | None) -> str:
    if not code:
        return "—"
    for c, label in LANGUAGES:
        if c == code:
            return label
    return code


# (code, label). `code` is the ISO-4217 currency code (uppercase canonical),
# stored lowercase in DB to match historical convention.
CURRENCIES: list[tuple[str, str]] = [
    ("rub", "🇷🇺 ₽ Рубль"),
    ("uah", "🇺🇦 ₴ Гривна"),
    ("byn", "🇧🇾 Br Бел. рубль"),
    ("kzt", "🇰🇿 ₸ Тенге"),
    ("eur", "🇪🇺 € Евро"),
    ("usd", "🇺🇸 $ Доллар"),
    ("pln", "🇵🇱 zł Злотый"),
    ("cny", "🇨🇳 ¥ Юань"),
]

CURRENCY_CODES = frozenset(c for c, _ in CURRENCIES)


def currency_label(code: str | None) -> str:
    if not code:
        return "—"
    for c, label in CURRENCIES:
        if c == code:
            return label
    return code.upper()


# (IANA timezone name, picker label). Curated list of ~20 popular zones
# rather than all 400+ — anyone outside this set keeps the default
# resolved from their language. Offsets shown are standard time; DST
# adjustments are handled automatically by zoneinfo at render time.
TIMEZONES: list[tuple[str, str]] = [
    ("Europe/Moscow",    "🇷🇺 Москва (UTC+3)"),
    ("Europe/Minsk",     "🇧🇾 Минск (UTC+3)"),
    ("Europe/Kyiv",      "🇺🇦 Киев (UTC+2)"),
    ("Asia/Almaty",      "🇰🇿 Алматы (UTC+5)"),
    ("Europe/London",    "🇬🇧 Лондон (UTC+0)"),
    ("Europe/Madrid",    "🇪🇸 Мадрид (UTC+1)"),
    ("Europe/Berlin",    "🇩🇪 Берлин (UTC+1)"),
    ("Europe/Paris",     "🇫🇷 Париж (UTC+1)"),
    ("Europe/Rome",      "🇮🇹 Рим (UTC+1)"),
    ("Europe/Warsaw",    "🇵🇱 Варшава (UTC+1)"),
    ("Europe/Lisbon",    "🇵🇹 Лиссабон (UTC+0)"),
    ("Europe/Amsterdam", "🇳🇱 Амстердам (UTC+1)"),
    ("Europe/Istanbul",  "🇹🇷 Стамбул (UTC+3)"),
    ("Europe/Bucharest", "🇷🇴 Бухарест (UTC+2)"),
    ("Europe/Prague",    "🇨🇿 Прага (UTC+1)"),
    ("Europe/Budapest",  "🇭🇺 Будапешт (UTC+1)"),
    ("Europe/Athens",    "🇬🇷 Афины (UTC+2)"),
    ("Europe/Sofia",     "🇧🇬 София (UTC+2)"),
    ("Europe/Stockholm", "🇸🇪 Стокгольм (UTC+1)"),
    ("Asia/Tokyo",       "🇯🇵 Токио (UTC+9)"),
    ("Asia/Shanghai",    "🇨🇳 Шанхай (UTC+8)"),
]

TIMEZONE_CODES = frozenset(c for c, _ in TIMEZONES)


# Maps the picker language code to the timezone we'll auto-assign when
# the user hasn't picked one explicitly. Anything missing falls through
# to Europe/Moscow.
DEFAULT_TZ_FOR_LANG: dict[str, str] = {
    "ru": "Europe/Moscow",
    "be": "Europe/Minsk",
    "uk": "Europe/Kyiv",
    "kk": "Asia/Almaty",
    "en": "Europe/London",
    "es": "Europe/Madrid",
    "de": "Europe/Berlin",
    "fr": "Europe/Paris",
    "it": "Europe/Rome",
    "pl": "Europe/Warsaw",
    "pt": "Europe/Lisbon",
    "nl": "Europe/Amsterdam",
    "tr": "Europe/Istanbul",
    "ro": "Europe/Bucharest",
    "cs": "Europe/Prague",
    "hu": "Europe/Budapest",
    "el": "Europe/Athens",
    "bg": "Europe/Sofia",
    "sv": "Europe/Stockholm",
}


def default_tz_for_lang(lang: str | None) -> str:
    if not lang:
        return "Europe/Moscow"
    return DEFAULT_TZ_FOR_LANG.get(lang.lower(), "Europe/Moscow")


def timezone_label(code: str | None) -> str:
    if not code:
        return "—"
    for c, label in TIMEZONES:
        if c == code:
            return label
    # Fall through: render the city slice ("Asia/Tashkent" → "Tashkent").
    return code.split("/")[-1].replace("_", " ")


# Short tag rendered next to the date in notifications. Picked to be
# recognisable at a glance without bloating the line; falls back to
# the city slice for zones not in the curated list.
_TIMEZONE_SHORT: dict[str, str] = {
    "Europe/Moscow":    "МСК",
    "Europe/Minsk":     "Минск",
    "Europe/Kyiv":      "Киев",
    "Asia/Almaty":      "Алматы",
    "Europe/London":    "Лондон",
    "Europe/Madrid":    "Мадрид",
    "Europe/Berlin":    "Берлин",
    "Europe/Paris":     "Париж",
    "Europe/Rome":      "Рим",
    "Europe/Warsaw":    "Варшава",
    "Europe/Lisbon":    "Лиссабон",
    "Europe/Amsterdam": "Амстердам",
    "Europe/Istanbul":  "Стамбул",
    "Europe/Bucharest": "Бухарест",
    "Europe/Prague":    "Прага",
    "Europe/Budapest":  "Будапешт",
    "Europe/Athens":    "Афины",
    "Europe/Sofia":     "София",
    "Europe/Stockholm": "Стокгольм",
    "Asia/Tokyo":       "Токио",
    "Asia/Shanghai":    "Шанхай",
}


def timezone_short(code: str | None) -> str:
    if not code:
        return "МСК"
    short = _TIMEZONE_SHORT.get(code)
    if short:
        return short
    return code.split("/")[-1].replace("_", " ")


def language_keyboard(back_callback: str | None = None) -> InlineKeyboardMarkup:
    """2-column grid of language buttons.

    If `back_callback` is given, an extra «⬅️ Назад» row is appended —
    used when entering the picker from profile (so the user can cancel
    without picking). Onboarding leaves it off so the user must choose.
    """
    rows = []
    row = []
    for code, label in LANGUAGES:
        row.append(InlineKeyboardButton(
            text=label, callback_data=f"setlang:{code}",
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if back_callback:
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def currency_keyboard(back_callback: str | None = None) -> InlineKeyboardMarkup:
    """2-column grid of currency buttons + optional «⬅️ Назад»."""
    rows = []
    row = []
    for code, label in CURRENCIES:
        row.append(InlineKeyboardButton(
            text=label, callback_data=f"setcur:{code}",
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if back_callback:
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Top-level inline menu.

    Layout:
        ➕ Добавить поиск
        📋 Мои поиски   | 👤 Профиль
        💎 Тарифы
        ❓ Помощь

    Settings (language / currency) lives inside the Profile screen now,
    so the main grid stays uncluttered.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить поиск", callback_data="menu:add")],
        [
            InlineKeyboardButton(text="📋 Мои поиски",  callback_data="menu:list"),
            InlineKeyboardButton(text="👤 Профиль",     callback_data="menu:profile"),
        ],
        [InlineKeyboardButton(text="💎 Тарифы",         callback_data="menu:tariffs")],
        [InlineKeyboardButton(text="❓ Помощь",         callback_data="menu:help")],
    ])


def timezone_keyboard(back_callback: str | None = None) -> InlineKeyboardMarkup:
    """1-column grid (city names + offsets are wider than lang/cur
    labels so two columns would wrap awkwardly on narrow phones)."""
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"settz:{code}")]
        for code, label in TIMEZONES
    ]
    if back_callback:
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:home")],
    ])
