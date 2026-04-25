"""Telegram bot handlers — aiogram 3.

UX flow:

    /start
      ├── (new user)  → onboarding wizard (language → currency) → main menu
      └── (returning) → main menu

    main menu (inline grid)
      ├── ➕ Добавить поиск    → URL-paste hint
      ├── 📋 Мои поиски        → list of active subs
      ├── 👤 Профиль           → user stats
      ├── 💎 Тарифы            → 5-tier paywall
      ├── ⚙️ Настройки         → re-pick language / currency
      └── ❓ Помощь            → quickstart text

Slash commands (/list /profile /settings /help / etc.) still work for
power users; they short-circuit straight to the relevant submenu.
"""
import logging
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot_i18n import (
    LANGUAGE_CODES, CURRENCY_CODES,
    language_label, currency_label,
    language_keyboard, currency_keyboard,
    main_menu_keyboard, back_to_menu_keyboard,
)
from config import config
from database import db
from parser import detect_source, fetch_search_items, supported_sources
from parsers import source_display_name
from parsers.common import proxy_for_source

logger = logging.getLogger(__name__)
router = Router()

_GENERIC_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------

def _extract_marketplace_url(message_or_text) -> tuple[str, str] | None:
    """Pull a supported-marketplace URL out of the raw message text/caption.

    Returns (source_name, url) on success, None otherwise. The URL is
    accepted only if one of the registered sources matches it (avito,
    kufar, olx, vinted, mercari, ...).

    Reads from message.text / message.caption directly; never from
    message.entities (clipped for long URLs).
    """
    if isinstance(message_or_text, str):
        text = message_or_text
    else:
        text = (
            getattr(message_or_text, "text", None)
            or getattr(message_or_text, "caption", None)
            or ""
        )
    if not text:
        return None

    # Strip invisible unicode that iOS/Android Telegram sometimes injects
    for ch in ("​", "‌", "‍", "⁠", "­",
               "﻿", " ", " ", " "):
        text = text.replace(ch, "")
    text = text.strip()
    # NOTE: do NOT replace ~ with - here — Avito's f= alphabet treats
    # them as different characters; the same is true for some other
    # marketplaces' filter encodings.

    m = _GENERIC_URL_RE.search(text)
    if not m:
        return None
    url = m.group(0).rstrip(".,);]")

    source = detect_source(url)
    if source is None:
        return None

    head = url[:50]
    tail = url[-50:] if len(url) > 50 else ""
    logger.info(
        "[extract-url] source=%s, len=%d, head=%r, tail=%r",
        source.name, len(url), head, tail,
    )
    return source.name, url


def _extract_avito_url(message_or_text) -> str | None:
    """Back-compat shim — older callers expected just the URL string."""
    result = _extract_marketplace_url(message_or_text)
    return result[1] if result else None


# ---------------------------------------------------------------------------
# Onboarding wizard + main menu
# ---------------------------------------------------------------------------

_HERO_TEXT = (
    "👋 <b>Добро пожаловать в AutoSearch!</b>\n\n"
    "Я мониторю Avito, OLX, Vinted, Kufar, Mercari и другие площадки и "
    "присылаю новые объявления в реальном времени.\n\n"
    "Чтобы я понимал, как тебе удобно получать уведомления, выбери "
    "<b>язык</b> и <b>валюту</b> — это займёт 10 секунд."
)

_LANG_PROMPT = "🌐 <b>Выбери язык</b>\nНа этом языке будут приходить объявления."

_CUR_PROMPT = (
    "💱 <b>Выбери валюту</b>\n"
    "Цены будут показываться в этой валюте (исходная цена остаётся, рядом — "
    "конвертация)."
)


async def _send_main_menu(message: Message, prefs: dict | None = None):
    if prefs is None:
        user_id = await db.get_or_create_user(
            message.from_user.id, message.from_user.username,
        )
        prefs = await db.get_user_prefs(user_id)
    text = (
        "🏠 <b>Главное меню</b>\n\n"
        f"Язык: {language_label(prefs['lang'])}\n"
        f"Валюта: {currency_label(prefs['currency'])}"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu_keyboard())


@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = await db.get_or_create_user(
        message.from_user.id, message.from_user.username,
    )
    # Silently reactivate any paused subs — returning users don't need to
    # re-do /start clicks to get their monitor back.
    await db.reactivate_all(user_id)

    prefs = await db.get_user_prefs(user_id)
    if prefs.get("onboarded"):
        await _send_main_menu(message, prefs)
        return

    # First-time flow: hero card → language picker.
    await message.answer(
        _HERO_TEXT,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚀 Поехали", callback_data="onboard:lang"),
        ]]),
    )


@router.callback_query(F.data == "onboard:lang")
async def callback_onboard_lang(callback: CallbackQuery):
    await callback.message.answer(
        _LANG_PROMPT, parse_mode="HTML", reply_markup=language_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("setlang:"))
async def callback_set_lang(callback: CallbackQuery):
    lang = callback.data.split(":", 1)[1]
    if lang not in LANGUAGE_CODES:
        await callback.answer("Неизвестный язык", show_alert=True)
        return
    user_id = await db.get_or_create_user(
        callback.from_user.id, callback.from_user.username,
    )
    await db.set_user_lang(user_id, lang)
    prefs = await db.get_user_prefs(user_id)
    await callback.answer(f"Язык: {language_label(lang)}")
    if prefs.get("onboarded"):
        # Settings change after initial onboarding — return to settings menu.
        await _send_settings_menu(callback.message, prefs)
    else:
        # Continue onboarding into currency picker.
        await callback.message.answer(
            _CUR_PROMPT, parse_mode="HTML", reply_markup=currency_keyboard(),
        )


@router.callback_query(F.data.startswith("setcur:"))
async def callback_set_currency(callback: CallbackQuery):
    cur = callback.data.split(":", 1)[1]
    if cur not in CURRENCY_CODES:
        await callback.answer("Неизвестная валюта", show_alert=True)
        return
    user_id = await db.get_or_create_user(
        callback.from_user.id, callback.from_user.username,
    )
    await db.set_user_currency(user_id, cur)
    prefs = await db.get_user_prefs(user_id)
    await callback.answer(f"Валюта: {currency_label(cur)}")
    if prefs.get("onboarded"):
        await _send_settings_menu(callback.message, prefs)
    else:
        # End of onboarding — flag and drop into the main menu.
        await db.set_user_onboarded(user_id, True)
        prefs["onboarded"] = True
        await _send_main_menu(callback.message, prefs)


# ---------------------------------------------------------------------------
# Main menu callback router
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu:home")
async def callback_menu_home(callback: CallbackQuery):
    await _send_main_menu(callback.message)
    await callback.answer()


@router.callback_query(F.data == "menu:add")
async def callback_menu_add(callback: CallbackQuery):
    sources_pretty = ", ".join(source_display_name(s) for s in supported_sources())
    await callback.message.answer(
        "➕ <b>Добавить поиск</b>\n\n"
        "Открой нужный маркетплейс, настрой фильтры и пришли мне ссылку "
        "со страницы поиска.\n\n"
        f"<b>Поддерживаемые площадки:</b> <i>{sources_pretty}</i>\n\n"
        "Пример:\n"
        "<code>https://www.olx.pl/d/oferty/q-iphone-13/</code>",
        parse_mode="HTML",
        reply_markup=back_to_menu_keyboard(),
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data == "menu:list")
async def callback_menu_list(callback: CallbackQuery):
    await _send_subscription_list(callback.message)
    await callback.answer()


@router.callback_query(F.data == "menu:profile")
async def callback_menu_profile(callback: CallbackQuery):
    await _send_profile(callback.message)
    await callback.answer()


@router.callback_query(F.data == "menu:tariffs")
async def callback_menu_tariffs(callback: CallbackQuery):
    await _send_tariffs(callback.message)
    await callback.answer()


@router.callback_query(F.data == "menu:settings")
async def callback_menu_settings(callback: CallbackQuery):
    user_id = await db.get_or_create_user(
        callback.from_user.id, callback.from_user.username,
    )
    prefs = await db.get_user_prefs(user_id)
    await _send_settings_menu(callback.message, prefs)
    await callback.answer()


@router.callback_query(F.data == "menu:help")
async def callback_menu_help(callback: CallbackQuery):
    await _send_help(callback.message)
    await callback.answer()


@router.callback_query(F.data == "settings:lang")
async def callback_settings_lang(callback: CallbackQuery):
    await callback.message.answer(
        _LANG_PROMPT, parse_mode="HTML", reply_markup=language_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "settings:cur")
async def callback_settings_cur(callback: CallbackQuery):
    await callback.message.answer(
        _CUR_PROMPT, parse_mode="HTML", reply_markup=currency_keyboard(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Submenus
# ---------------------------------------------------------------------------

# (id, label_emoji+name, price_pretty, limits, footnote_or_None)
_TARIFFS = [
    ("trial",    "🎁 Пробный",          "Бесплатно",     "1 поиск, 6 часов",   "(только один раз)"),
    ("intro",    "🚀 Ознакомительный",  "69 ₽",          "1 поиск, 24 часа",   None),
    ("basic",    "💎 Базовый",          "890 ₽/мес",     "1 поиск, 30 дней",   None),
    ("advanced", "⚡ Продвинутый",       "1 790 ₽/мес",   "3 поиска, 30 дней",  None),
    ("pro",      "👑 Профессиональный", "2 590 ₽/мес",   "5 поисков, 30 дней", None),
]


async def _send_tariffs(message: Message):
    lines = [
        "💎 <b>Тарифы AutoSearch</b>\n",
    ]
    for tid, name, price, limits, foot in _TARIFFS:
        line = f"<b>{name}</b> — {price}\n   {limits}"
        if foot:
            line += f"\n   <i>{foot}</i>"
        lines.append(line)
    lines.append("\nВыбери тариф для оформления:")

    buttons = []
    for tid, name, price, _, _ in _TARIFFS:
        buttons.append([InlineKeyboardButton(
            text=f"{name} — {price}",
            callback_data=f"buy:{tid}",
        )])
    buttons.append([InlineKeyboardButton(
        text="⬅️ Главное меню", callback_data="menu:home",
    )])

    await message.answer(
        "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("buy:"))
async def callback_buy_tariff(callback: CallbackQuery):
    tariff_id = callback.data.split(":", 1)[1]
    meta = next((t for t in _TARIFFS if t[0] == tariff_id), None)
    if meta is None:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return
    _, name, price, limits, _ = meta

    if config.support_handle:
        contact = (
            f"Для оформления напиши {config.support_handle} — оплату подключим "
            "и активируем тариф."
        )
    else:
        contact = "Оплата временно недоступна. Попробуйте позже."

    await callback.message.answer(
        f"<b>{name}</b>\n\n"
        f"💰 Стоимость: <b>{price}</b>\n"
        f"📦 Что входит: <b>{limits}</b>\n\n"
        f"{contact}",
        parse_mode="HTML",
        reply_markup=back_to_menu_keyboard(),
    )
    await callback.answer()


async def _send_settings_menu(message: Message, prefs: dict):
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"🌐 Язык: <b>{language_label(prefs['lang'])}</b>\n"
        f"💱 Валюта: <b>{currency_label(prefs['currency'])}</b>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Сменить язык",   callback_data="settings:lang")],
        [InlineKeyboardButton(text="💱 Сменить валюту", callback_data="settings:cur")],
        [InlineKeyboardButton(text="⬅️ Главное меню",   callback_data="menu:home")],
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


async def _send_help(message: Message):
    text = (
        "❓ <b>Как это работает</b>\n\n"
        "1. Открой нужный маркетплейс и настрой фильтры (бренд, размер, "
        "ценовой диапазон, регион).\n"
        "2. Скопируй URL страницы поиска и пришли его мне.\n"
        "3. Я буду каждую минуту проверять новые объявления и присылать их "
        "сюда — с фото, ценой в твоей валюте, локацией и описанием.\n\n"
        "<b>Команды:</b>\n"
        "/start — главное меню\n"
        "/list — мои поиски\n"
        "/profile — профиль и статистика\n"
        "/settings — язык / валюта\n"
        "/stop — приостановить все поиски\n"
        "/help — эта справка"
    )
    await message.answer(text, parse_mode="HTML",
                         reply_markup=back_to_menu_keyboard())


# ---------------------------------------------------------------------------
# Slash commands (also reusable from inline buttons)
# ---------------------------------------------------------------------------

async def _send_subscription_list(message: Message):
    user_id = await db.get_or_create_user(
        message.from_user.id, message.from_user.username,
    )
    subs = await db.get_user_subscriptions(user_id)
    active = [s for s in subs if s["is_active"]]

    if not active:
        await message.answer(
            "📋 <b>У тебя нет активных поисков.</b>\n\n"
            "Пришли ссылку на поиск с любого поддерживаемого маркетплейса — "
            "я начну мониторить.",
            parse_mode="HTML",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    lines = ["📋 <b>Активные поиски:</b>\n"]
    for i, sub in enumerate(active, 1):
        checked = sub["last_checked_at"]
        checked_str = checked.strftime("%d.%m %H:%M") if checked else "ещё не проверялась"
        errors = f" ⚠️ ошибок: {sub['error_count']}" if sub["error_count"] > 0 else ""
        lines.append(
            f"{i}. <a href=\"{sub['url']}\">Поиск #{sub['id']}</a>\n"
            f"   Последняя проверка: {checked_str}{errors}"
        )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Удалить поиск",  callback_data="cmd:delete")],
        [InlineKeyboardButton(text="⬅️ Главное меню",   callback_data="menu:home")],
    ])
    await message.answer(
        "\n".join(lines), parse_mode="HTML",
        disable_web_page_preview=True, reply_markup=keyboard,
    )


async def _send_profile(message: Message):
    user_id = await db.get_or_create_user(
        message.from_user.id, message.from_user.username,
    )
    profile = await db.get_user_profile(user_id)
    user = profile["user"]

    reg_date = user["created_at"].strftime("%d.%m.%Y") if user and user["created_at"] else "—"
    last_found_str = "—"
    if profile["last_found"]:
        last_found_str = profile["last_found"]["sent_at"].strftime("%d.%m.%Y %H:%M")

    name = message.from_user.full_name or (user and user["username"]) or "Пользователь"

    await message.answer(
        f"👤 <b>Профиль: {name}</b>\n\n"
        f"📅 Регистрация: <b>{reg_date}</b>\n"
        f"📊 Всего поисков: <b>{profile['total_subs']}</b>\n"
        f"🟢 Активных сейчас: <b>{profile['active_subs']}</b>\n"
        f"📨 Объявлений найдено: <b>{profile['total_found']}</b>\n"
        f"🕐 Последнее найденное: <b>{last_found_str}</b>",
        parse_mode="HTML",
        reply_markup=back_to_menu_keyboard(),
    )


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    await _send_profile(message)


@router.message(Command("settings"))
async def cmd_settings(message: Message):
    user_id = await db.get_or_create_user(
        message.from_user.id, message.from_user.username,
    )
    prefs = await db.get_user_prefs(user_id)
    await _send_settings_menu(message, prefs)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await _send_help(message)


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if config.admin_id == 0 or message.from_user.id != config.admin_id:
        return

    stats = await db.get_admin_stats()

    last_checked_str = "—"
    if stats["last_checked"]:
        from datetime import timedelta, timezone as tz
        msk = tz(timedelta(hours=3))
        last_checked_str = stats["last_checked"].astimezone(msk).strftime("%H:%M %d.%m.%Y")

    await message.answer(
        f"📊 <b>Админ-панель</b>\n\n"
        f"👥 Юзеров: <b>{stats['total_users']:,}</b>\n"
        f"📋 Активных подписок: <b>{stats['active_subs']:,}</b>\n"
        f"🔗 Уникальных ссылок: <b>{stats['unique_urls']:,}</b>\n"
        f"📨 Объявлений отправлено: <b>{stats['total_sent']:,}</b>\n"
        f"⏱ Последняя проверка: <b>{last_checked_str}</b>\n\n"
        f"📈 <b>За 24ч:</b>\n"
        f"  Новых юзеров: <b>{stats['new_users_24h']:,}</b>\n"
        f"  Отправлено: <b>{stats['sent_24h']:,}</b>",
        parse_mode="HTML",
    )


@router.message(Command("list"))
async def cmd_list(message: Message):
    await _send_subscription_list(message)


@router.message(Command("delete"))
async def cmd_delete(message: Message):
    user_id = await db.get_or_create_user(
        message.from_user.id, message.from_user.username,
    )
    subs = await db.get_user_subscriptions(user_id)
    active = [s for s in subs if s["is_active"]]

    if not active:
        await message.answer(
            "Нет активных поисков для удаления.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    buttons = []
    for sub in active:
        short = sub["url"][:50] + "..." if len(sub["url"]) > 50 else sub["url"]
        buttons.append([
            InlineKeyboardButton(
                text=f"❌ #{sub['id']} — {short}",
                callback_data=f"del:{sub['id']}",
            )
        ])
    buttons.append([InlineKeyboardButton(
        text="⬅️ Главное меню", callback_data="menu:home",
    )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Выбери поиск для удаления:", reply_markup=keyboard)


@router.callback_query(F.data == "cmd:delete")
async def callback_cmd_delete(callback: CallbackQuery):
    await cmd_delete(callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("del:"))
async def callback_delete(callback: CallbackQuery):
    try:
        sub_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID")
        return
    await db.deactivate_subscription(sub_id)
    await callback.message.edit_text(f"✅ Поиск #{sub_id} удалён.")
    await callback.answer()


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    user_id = await db.get_or_create_user(
        message.from_user.id, message.from_user.username,
    )
    await db.deactivate_all(user_id)
    await message.answer(
        "⏸ <b>Мониторинг приостановлен</b>\n\n"
        "Все ссылки сохранены. Нажми /start чтобы возобновить.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Free-text URL handler — main "add subscription" entry point
# ---------------------------------------------------------------------------

@router.message(F.text)
async def handle_url(message: Message):
    extracted = _extract_marketplace_url(message)
    if not extracted:
        sources_pretty = ", ".join(source_display_name(s) for s in supported_sources())
        await message.answer(
            "Отправь ссылку на поиск с одного из поддерживаемых сайтов:\n"
            f"<i>{sources_pretty}</i>\n\n"
            "Например: <code>https://www.olx.pl/d/oferty/q-iphone-13/</code>",
            parse_mode="HTML",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    source_name, url = extracted

    user_id = await db.get_or_create_user(
        message.from_user.id, message.from_user.username,
    )

    sub_id = await db.add_subscription(user_id, url, source=source_name)
    if sub_id is None:
        await message.answer(
            f"⚠️ Достигнут лимит — максимум {config.max_subscriptions} поисков.\n"
            "Удали лишние через 📋 «Мои поиски» → ❌ Удалить.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    pretty = source_display_name(source_name)
    await message.answer(
        f"⏳ <b>Поиск #{sub_id} добавлен</b> ({pretty})\n"
        f"Открываю страницу и записываю текущие объявления...",
        parse_mode="HTML",
    )
    try:
        proxy = proxy_for_source(source_name)
        initial_items = await fetch_search_items(url, proxy)
    except Exception:
        logger.exception("initial scan failed for sub #%d", sub_id)
        initial_items = None

    if initial_items:
        ids = [i.external_id for i in initial_items if i.external_id]
        await db.mark_items_sent_batch(sub_id, ids, source=source_name)
        await db.update_last_checked(sub_id)
        seeded = len(ids)
    else:
        seeded = 0

    await message.answer(
        f"✅ <b>Мониторинг запущен</b>\n\n"
        f"🔗 <a href=\"{url}\">Твоя ссылка на {pretty}</a>\n\n"
        f"Записал {seeded} текущих объявлений как уже виденные. "
        f"Как появится новое — пришлю с фото, ценой и описанием.",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=main_menu_keyboard(),
    )
