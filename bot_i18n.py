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
_TIMEZONE_SHORT_RU: dict[str, str] = {
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

# English city labels for non-RU users. The naïve approach of running
# the RU labels through Google Translate per-notification gave us
# "(Шанхай)" in the middle of an Italian message body — see the
# notification rendering bug from 2026-04-29. English city names are
# universally readable in EU/JP locales and don't require a network
# round-trip.
_TIMEZONE_SHORT_EN: dict[str, str] = {
    "Europe/Moscow":    "Moscow",
    "Europe/Minsk":     "Minsk",
    "Europe/Kyiv":      "Kyiv",
    "Asia/Almaty":      "Almaty",
    "Europe/London":    "London",
    "Europe/Madrid":    "Madrid",
    "Europe/Berlin":    "Berlin",
    "Europe/Paris":     "Paris",
    "Europe/Rome":      "Rome",
    "Europe/Warsaw":    "Warsaw",
    "Europe/Lisbon":    "Lisbon",
    "Europe/Amsterdam": "Amsterdam",
    "Europe/Istanbul":  "Istanbul",
    "Europe/Bucharest": "Bucharest",
    "Europe/Prague":    "Prague",
    "Europe/Budapest":  "Budapest",
    "Europe/Athens":    "Athens",
    "Europe/Sofia":     "Sofia",
    "Europe/Stockholm": "Stockholm",
    "Asia/Tokyo":       "Tokyo",
    "Asia/Shanghai":    "Shanghai",
}

# Cyrillic-script langs share the RU labels (close-enough orthography);
# everyone else gets EN city names.
_CYRILLIC_LANGS = frozenset({"ru", "be", "uk", "bg", "kk"})


def timezone_short(code: str | None, lang: str | None = "ru") -> str:
    """Return a short city tag for `code` in the user's display language.

    Falls back to the IANA city slice ("New_York" → "New York") when
    the code isn't curated."""
    if not code:
        return "МСК" if (lang or "ru").lower() in _CYRILLIC_LANGS else "Moscow"
    table = _TIMEZONE_SHORT_RU if (lang or "ru").lower() in _CYRILLIC_LANGS else _TIMEZONE_SHORT_EN
    short = table.get(code)
    if short:
        return short
    return code.split("/")[-1].replace("_", " ")


# Back-compat alias for any callers that still imported the dict
# directly. Keeping it pointed at the RU table preserves behaviour
# from before the lang-aware split.
_TIMEZONE_SHORT = _TIMEZONE_SHORT_RU


# Date-line phrasing per language. Three patterns each:
#   "today"     — used when the listing was published earlier today
#   "yesterday" — published the previous calendar day
#   "date"      — older items, with explicit DD.MM date
# Placeholders: {time} = HH:MM, {tz} = short city tag, {date} = DD.MM.
# Languages without an entry fall back to RU (the bot's defaults).
_DATE_TEMPLATES: dict[str, dict[str, str]] = {
    "ru": {"today": "Сегодня в {time} ({tz})",
           "yesterday": "Вчера в {time} ({tz})",
           "date": "{date} в {time} ({tz})"},
    "be": {"today": "Сёння ў {time} ({tz})",
           "yesterday": "Учора ў {time} ({tz})",
           "date": "{date} ў {time} ({tz})"},
    "uk": {"today": "Сьогодні о {time} ({tz})",
           "yesterday": "Вчора о {time} ({tz})",
           "date": "{date} о {time} ({tz})"},
    "kk": {"today": "Бүгін {time} ({tz})",
           "yesterday": "Кеше {time} ({tz})",
           "date": "{date}, {time} ({tz})"},
    "en": {"today": "Today at {time} ({tz})",
           "yesterday": "Yesterday at {time} ({tz})",
           "date": "{date} at {time} ({tz})"},
    "es": {"today": "Hoy a las {time} ({tz})",
           "yesterday": "Ayer a las {time} ({tz})",
           "date": "{date} a las {time} ({tz})"},
    "de": {"today": "Heute um {time} ({tz})",
           "yesterday": "Gestern um {time} ({tz})",
           "date": "{date} um {time} ({tz})"},
    "fr": {"today": "Aujourd'hui à {time} ({tz})",
           "yesterday": "Hier à {time} ({tz})",
           "date": "{date} à {time} ({tz})"},
    "it": {"today": "Oggi alle {time} ({tz})",
           "yesterday": "Ieri alle {time} ({tz})",
           "date": "{date} alle {time} ({tz})"},
    "pl": {"today": "Dziś o {time} ({tz})",
           "yesterday": "Wczoraj o {time} ({tz})",
           "date": "{date} o {time} ({tz})"},
    "pt": {"today": "Hoje às {time} ({tz})",
           "yesterday": "Ontem às {time} ({tz})",
           "date": "{date} às {time} ({tz})"},
    "nl": {"today": "Vandaag om {time} ({tz})",
           "yesterday": "Gisteren om {time} ({tz})",
           "date": "{date} om {time} ({tz})"},
    "tr": {"today": "Bugün {time} ({tz})",
           "yesterday": "Dün {time} ({tz})",
           "date": "{date}, {time} ({tz})"},
    "ro": {"today": "Astăzi la {time} ({tz})",
           "yesterday": "Ieri la {time} ({tz})",
           "date": "{date} la {time} ({tz})"},
    "cs": {"today": "Dnes v {time} ({tz})",
           "yesterday": "Včera v {time} ({tz})",
           "date": "{date} v {time} ({tz})"},
    "hu": {"today": "Ma {time}-kor ({tz})",
           "yesterday": "Tegnap {time}-kor ({tz})",
           "date": "{date}, {time} ({tz})"},
    "el": {"today": "Σήμερα στις {time} ({tz})",
           "yesterday": "Χθες στις {time} ({tz})",
           "date": "{date} στις {time} ({tz})"},
    "bg": {"today": "Днес в {time} ({tz})",
           "yesterday": "Вчера в {time} ({tz})",
           "date": "{date} в {time} ({tz})"},
    "sv": {"today": "Idag kl {time} ({tz})",
           "yesterday": "Igår kl {time} ({tz})",
           "date": "{date} kl {time} ({tz})"},
}


def date_template(lang: str | None, kind: str) -> str:
    """Return the «today / yesterday / date» template for a language.

    Falls back to Russian phrasing when the language isn't on the
    supported-list or `kind` isn't one of {today, yesterday, date}."""
    table = _DATE_TEMPLATES.get((lang or "ru").lower(), _DATE_TEMPLATES["ru"])
    return table.get(kind, _DATE_TEMPLATES["ru"][kind])


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
