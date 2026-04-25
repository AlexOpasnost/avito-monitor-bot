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


def language_keyboard() -> InlineKeyboardMarkup:
    """2-column grid of language buttons."""
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
    return InlineKeyboardMarkup(inline_keyboard=rows)


def currency_keyboard() -> InlineKeyboardMarkup:
    """2-column grid of currency buttons."""
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
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Top-level inline menu, top-bot style.

    Layout:
        ➕ Добавить поиск           (full row — primary action)
        📋 Мои поиски  | 👤 Профиль (paired)
        💎 Тарифы      | ⚙️ Настройки
        ❓ Помощь                   (full row)
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить поиск", callback_data="menu:add")],
        [
            InlineKeyboardButton(text="📋 Мои поиски",  callback_data="menu:list"),
            InlineKeyboardButton(text="👤 Профиль",     callback_data="menu:profile"),
        ],
        [
            InlineKeyboardButton(text="💎 Тарифы",      callback_data="menu:tariffs"),
            InlineKeyboardButton(text="⚙️ Настройки",   callback_data="menu:settings"),
        ],
        [InlineKeyboardButton(text="❓ Помощь",         callback_data="menu:help")],
    ])


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:home")],
    ])
