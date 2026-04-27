"""Telegram bot handlers — aiogram 3.

UX flow:

    /start
      ├── (new user)  → onboarding wizard (language → currency) → main menu
      └── (returning) → main menu

    main menu (inline grid)
      ├── ➕ Добавить поиск   → URL-paste hint
      ├── 📋 Мои поиски        → list of active subs
      ├── 👤 Профиль           → stats + change language / currency
      ├── 💎 Тарифы            → 5-tier paywall
      └── ❓ Помощь            → quickstart text

Inline navigation edits the existing menu message in place — clicking
buttons morphs the same panel rather than spamming new messages. Slash
commands and free-text URL pastes still send fresh messages.

Slash commands (/list /profile /settings /help / ...) work for power
users; they short-circuit straight to the relevant submenu.
"""
import html as _html
import json
import logging
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from datetime import datetime, timezone

from services import yookassa as yk

from bot_i18n import (
    LANGUAGE_CODES, CURRENCY_CODES, TIMEZONE_CODES,
    language_label, currency_label, timezone_label,
    language_keyboard, currency_keyboard, timezone_keyboard,
    main_menu_keyboard, back_to_menu_keyboard,
    default_tz_for_lang,
)
from config import config
from database import db
from parser import detect_source, fetch_search_items, supported_sources
from parsers import source_display_name
from parsers.common import proxy_for_source

logger = logging.getLogger(__name__)
router = Router()

_GENERIC_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# Max length of a user-set subscription label. Telegram caption /
# inline-button width get unhappy with anything much longer.
_MAX_NAME_LEN = 30


class RenameStates(StatesGroup):
    """FSM: user clicked ✏️, the next text message they send becomes
    the subscription's new name."""
    waiting_for_name = State()


class BuyStates(StatesGroup):
    """FSM: user clicked a paid tariff button, we asked for their
    email (required for the YooKassa receipt under Мой налог), the
    next text message they send is the email."""
    waiting_for_email = State()


# Loose email shape check — YooKassa does the real validation when
# it tries to send the receipt. We just gate against obvious
# mistypes (missing @, missing domain dot, whitespace).
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")


def _sub_display_name(sub: dict) -> str:
    """Custom name if set, otherwise a sensible default built from
    the marketplace name."""
    raw = sub.get("name")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return source_display_name(sub.get("source") or "")


# ---------------------------------------------------------------------------
# Render helper — edit-in-place vs send-fresh
# ---------------------------------------------------------------------------

async def _present(
    target, text: str, keyboard: InlineKeyboardMarkup | None = None,
):
    """Render `text` + `keyboard` to the user.

    - When `target` is a CallbackQuery (an inline-button press), edit the
      existing menu message so navigation morphs in place.
    - When `target` is a Message (slash command or free-text URL paste),
      send a fresh reply.

    Falls back to a fresh send if the edit fails (old message, photo
    caption, "message is not modified", etc.)
    """
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(
                text, parse_mode="HTML", reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return
        except TelegramBadRequest:
            await target.message.answer(
                text, parse_mode="HTML", reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return
    await target.answer(
        text, parse_mode="HTML", reply_markup=keyboard,
        disable_web_page_preview=True,
    )


def _user_from(target) -> tuple[int, str | None]:
    """Pull (telegram_id, username) regardless of whether `target` is a
    Message or a CallbackQuery."""
    user = target.from_user
    return user.id, user.username


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------

def _extract_marketplace_url(message_or_text) -> tuple[str, str] | None:
    """Pull a supported-marketplace URL out of the raw message text/caption.

    Returns (source_name, url) on success, None otherwise. The URL is
    accepted only if one of the registered sources matches it.

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

_TZ_PROMPT = (
    "🕐 <b>Выбери часовой пояс</b>\n"
    "В этом поясе будут показываться даты публикации объявлений.\n\n"
    "<i>По умолчанию выставляется из языка — меняй только если живёшь в "
    "другом часовом поясе.</i>"
)


async def _show_hero(target):
    await _present(
        target, _HERO_TEXT,
        keyboard=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚀 Поехали", callback_data="onboard:lang"),
        ]]),
    )


async def _show_lang_picker(target, *, back: str | None):
    """Show the language picker. `back` is the callback to return to
    when the user clicks «⬅️ Назад» — None during onboarding (no escape)
    or e.g. "menu:profile" when entering from the profile screen."""
    await _present(target, _LANG_PROMPT, keyboard=language_keyboard(back))


async def _show_currency_picker(target, *, back: str | None):
    await _present(target, _CUR_PROMPT, keyboard=currency_keyboard(back))


async def _show_tz_picker(target, *, back: str | None):
    await _present(target, _TZ_PROMPT, keyboard=timezone_keyboard(back))


async def _show_main_menu(target, prefs: dict | None = None):
    if prefs is None:
        tg_id, username = _user_from(target)
        user_id = await db.get_or_create_user(tg_id, username)
        prefs = await db.get_user_prefs(user_id)
    text = (
        "🏠 <b>Главное меню</b>\n\n"
        f"🌐 Язык: {language_label(prefs['lang'])}\n"
        f"💱 Валюта: {currency_label(prefs['currency'])}"
    )
    await _present(target, text, keyboard=main_menu_keyboard())


@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = await db.get_or_create_user(
        message.from_user.id, message.from_user.username,
    )
    # Silently reactivate any paused subs — returning users don't need
    # to re-do /start clicks to get their monitor back.
    await db.reactivate_all(user_id)

    prefs = await db.get_user_prefs(user_id)
    if prefs.get("onboarded"):
        await _show_main_menu(message, prefs)
    else:
        await _show_hero(message)


@router.callback_query(F.data == "onboard:lang")
async def callback_onboard_lang(callback: CallbackQuery):
    await _show_lang_picker(callback, back=None)
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
        # Settings change after initial onboarding — return to profile
        # (the screen the user came from).
        await _show_profile(callback, prefs=prefs)
    else:
        # Continue onboarding into the currency picker.
        await _show_currency_picker(callback, back=None)


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
        await _show_profile(callback, prefs=prefs)
    else:
        # End of onboarding — flag and drop into the main menu.
        await db.set_user_onboarded(user_id, True)
        prefs["onboarded"] = True
        await _show_main_menu(callback, prefs)


# ---------------------------------------------------------------------------
# Main menu callback router
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu:home")
async def callback_menu_home(callback: CallbackQuery):
    await _show_main_menu(callback)
    await callback.answer()


@router.callback_query(F.data == "menu:add")
async def callback_menu_add(callback: CallbackQuery):
    await _show_add_hint(callback)
    await callback.answer()


@router.callback_query(F.data == "menu:list")
async def callback_menu_list(callback: CallbackQuery):
    await _show_subscription_list(callback)
    await callback.answer()


@router.callback_query(F.data == "menu:profile")
async def callback_menu_profile(callback: CallbackQuery):
    await _show_profile(callback)
    await callback.answer()


@router.callback_query(F.data == "menu:tariffs")
async def callback_menu_tariffs(callback: CallbackQuery):
    await _show_tariffs(callback)
    await callback.answer()


@router.callback_query(F.data == "menu:help")
async def callback_menu_help(callback: CallbackQuery):
    await _show_help(callback)
    await callback.answer()


# Profile-side language / currency change buttons. These reuse the
# same picker callbacks; the picker comes back to profile because
# users.onboarded is already TRUE.
@router.callback_query(F.data == "profile:lang")
async def callback_profile_lang(callback: CallbackQuery):
    await _show_lang_picker(callback, back="menu:profile")
    await callback.answer()


@router.callback_query(F.data == "profile:cur")
async def callback_profile_cur(callback: CallbackQuery):
    await _show_currency_picker(callback, back="menu:profile")
    await callback.answer()


@router.callback_query(F.data == "profile:tz")
async def callback_profile_tz(callback: CallbackQuery):
    await _show_tz_picker(callback, back="menu:profile")
    await callback.answer()


@router.callback_query(F.data.startswith("settz:"))
async def callback_set_timezone(callback: CallbackQuery):
    tz = callback.data.split(":", 1)[1]
    if tz not in TIMEZONE_CODES:
        await callback.answer("Неизвестный часовой пояс", show_alert=True)
        return
    user_id = await db.get_or_create_user(
        callback.from_user.id, callback.from_user.username,
    )
    await db.set_user_timezone(user_id, tz)
    await callback.answer(f"Пояс: {timezone_label(tz)}")
    await _show_profile(callback)


# ---------------------------------------------------------------------------
# Submenus
# ---------------------------------------------------------------------------

# (id, label, price_pretty, limits, footnote_or_None)
# Single source of truth for tariff configuration. Each entry is the
# id used in DB rows + payment payloads, the human label, the price
# string for the UI, the limits string, an optional footnote, plus
# the machine-readable price/duration/sub-limit triplet.
_TARIFFS = [
    ("trial",    "🎁 Пробный",          "Бесплатно",     "1 поиск, 6 часов",   "(только один раз)"),
    ("basic",    "💎 Базовый",          "890 ₽/мес",     "1 поиск, 30 дней",   None),
    ("advanced", "⚡ Продвинутый",       "1 790 ₽/мес",   "3 поиска, 30 дней",  None),
    ("pro",      "👑 Профессиональный", "2 590 ₽/мес",   "5 поисков, 30 дней", None),
]

# tariff_id → (max_subs, hours, price_kopeks). `legacy` is the
# grandfathered tier auto-assigned to users who registered before the
# paywall existed. Free state (DB row is NULL or expired) maps to 0
# subs — user has to activate Trial or buy a tier.
_TARIFF_RULES: dict[str, dict] = {
    "trial":    {"max_subs": 1,   "hours": 6,        "kopeks": 0},
    "basic":    {"max_subs": 1,   "hours": 30 * 24,  "kopeks": 89000},
    "advanced": {"max_subs": 3,   "hours": 30 * 24,  "kopeks": 179000},
    "pro":      {"max_subs": 5,   "hours": 30 * 24,  "kopeks": 259000},
    # No expiry, max 5 — preserves behaviour for users who joined
    # before the paywall was introduced.
    "legacy":   {"max_subs": 5,   "hours": 0,        "kopeks": 0},
    # Bot admins (config.admin_ids). Bypasses paywall, virtually
    # unlimited search count. Resolved at runtime from is_admin(),
    # never persisted to DB — removing someone from ADMIN_IDS in
    # Railway env vars instantly demotes them.
    "admin":    {"max_subs": 999, "hours": 0,        "kopeks": 0},
}


def is_admin(telegram_id: int | None) -> bool:
    if not telegram_id:
        return False
    return telegram_id in (config.admin_ids or [])


def _tariff_meta(tariff_id: str | None) -> tuple | None:
    """Return the human row from _TARIFFS for a given id, or None."""
    if not tariff_id:
        return None
    return next((t for t in _TARIFFS if t[0] == tariff_id), None)


def _resolve_tariff_state(tariff_row: dict) -> dict:
    """Distill a raw users.tariff row into a render-ready snapshot.

    Returns:
        {active: bool, tariff_id: str|None, expires_at: datetime|None,
         max_subs: int, days_left: int|None, trial_used: bool}
    """
    tariff_id = tariff_row.get("tariff")
    expires_at = tariff_row.get("expires_at")
    trial_used = bool(tariff_row.get("trial_used"))

    rules = _TARIFF_RULES.get(tariff_id) if tariff_id else None
    # Legacy has no expiry — always active.
    if tariff_id == "legacy":
        return {
            "active": True, "tariff_id": "legacy", "expires_at": None,
            "max_subs": rules["max_subs"], "days_left": None,
            "trial_used": trial_used,
        }
    # Active = has a tariff and the expiry is still in the future.
    if rules and expires_at:
        now = datetime.now(timezone.utc)
        if expires_at > now:
            seconds_left = (expires_at - now).total_seconds()
            days_left = max(1, int(seconds_left // 86400))
            return {
                "active": True, "tariff_id": tariff_id,
                "expires_at": expires_at, "max_subs": rules["max_subs"],
                "days_left": days_left, "trial_used": trial_used,
            }
    # No active tariff — free state.
    return {
        "active": False, "tariff_id": None, "expires_at": None,
        "max_subs": 0, "days_left": None, "trial_used": trial_used,
    }


async def _user_tariff_state(user_id: int, telegram_id: int | None = None) -> dict:
    """Resolve the current tariff state for a user.

    Admin telegram_ids (per config.admin_ids) get a synthetic 'admin'
    state — no DB lookup, no expiry, max_subs=999. This means:
      - removing someone from ADMIN_IDS instantly drops them back to
        whatever their persisted tariff is (or free if never bought)
      - admins never leak into the paid-users dashboard
    """
    if telegram_id is not None and is_admin(telegram_id):
        rules = _TARIFF_RULES["admin"]
        return {
            "active": True, "tariff_id": "admin", "expires_at": None,
            "max_subs": rules["max_subs"], "days_left": None,
            "trial_used": True,  # hide trial button for admins
        }
    return _resolve_tariff_state(await db.get_user_tariff(user_id))


async def _show_tariffs(target):
    tg_id, username = _user_from(target)
    user_id = await db.get_or_create_user(tg_id, username)
    state = await _user_tariff_state(user_id, telegram_id=tg_id)

    header = "💎 <b>Тарифы AutoSearch</b>\n"
    if state["active"]:
        cur_meta = _tariff_meta(state["tariff_id"])
        cur_label = cur_meta[1] if cur_meta else state["tariff_id"]
        if state["days_left"] is not None:
            header += (
                f"\n<i>Сейчас активен: <b>{cur_label}</b> "
                f"(осталось ~{state['days_left']} дн.)</i>\n"
            )
        else:
            header += f"\n<i>Сейчас активен: <b>{cur_label}</b></i>\n"

    lines = [header]
    for tid, name, price, limits, foot in _TARIFFS:
        line = f"<b>{name}</b> — {price}\n   {limits}"
        if foot:
            line += f"\n   <i>{foot}</i>"
        # Mark trial as unavailable if already used.
        if tid == "trial" and state["trial_used"]:
            line += "\n   <i>✅ уже использован</i>"
        lines.append(line)
    lines.append("\nВыбери тариф для оформления:")

    buttons = []
    for tid, name, price, _, _ in _TARIFFS:
        # Hide the trial button after it's been used.
        if tid == "trial" and state["trial_used"]:
            continue
        buttons.append([InlineKeyboardButton(
            text=f"{name} — {price}", callback_data=f"buy:{tid}",
        )])
    buttons.append([InlineKeyboardButton(
        text="⬅️ Главное меню", callback_data="menu:home",
    )])
    await _present(
        target, "\n\n".join(lines),
        keyboard=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("buy:"))
async def callback_buy_tariff(callback: CallbackQuery, state: FSMContext):
    tariff_id = callback.data.split(":", 1)[1]
    meta = _tariff_meta(tariff_id)
    rules = _TARIFF_RULES.get(tariff_id)
    if meta is None or rules is None:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return

    # Admins bypass the paywall entirely — no need to send them an
    # invoice or activate Trial. Defensive: someone could replay the
    # callback URL even after the buttons were hidden in the UI.
    if is_admin(callback.from_user.id):
        await callback.answer(
            "У тебя админ-доступ — оплата не нужна, лимиты сняты.",
            show_alert=True,
        )
        return

    user_id = await db.get_or_create_user(
        callback.from_user.id, callback.from_user.username,
    )

    # Trial — free, one-shot, no Telegram-Payments invoice.
    if tariff_id == "trial":
        # The atomic UPDATE inside activate_tariff returns None when
        # the trial was already claimed (e.g. parallel double-click) —
        # caller never has to do its own race-prone read+check.
        new_exp = await db.activate_tariff(
            user_id, "trial", rules["hours"], is_trial=True,
        )
        if new_exp is None:
            await callback.answer(
                "Пробный уже был использован — выбери платный тариф.",
                show_alert=True,
            )
            return
        await callback.answer("Пробный активирован!")
        await _show_profile(callback)
        return

    # Paid tariff — fire a native Telegram invoice via the configured
    # provider (YooKassa). Falls back to a support-handle message if
    # the bot isn't wired up yet (no PAYMENT_PROVIDER_TOKEN set).
    if not config.payment_provider_token:
        contact = (
            f"Оплата временно недоступна. Напиши {config.support_handle}."
            if config.support_handle
            else "Оплата временно недоступна. Попробуйте позже."
        )
        _, name, price, limits, _ = meta
        text = (
            f"<b>{name}</b>\n\n"
            f"💰 Стоимость: <b>{price}</b>\n"
            f"📦 Что входит: <b>{limits}</b>\n\n{contact}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К тарифам",    callback_data="menu:tariffs")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
        ])
        await _present(callback, text, keyboard=keyboard)
        await callback.answer()
        return

    # Paid tariff via YooKassa REST API. The user clicks a button
    # that takes them to YooKassa's hosted payment page where they
    # pick СБП / card / SberPay / etc. After they pay, YooKassa
    # POSTs our /webhook/yookassa endpoint and that handler
    # activates the tariff (see webhook.py).
    if not (config.yookassa_shop_id and config.yookassa_secret_key
            and config.webhook_base_url):
        contact = (
            f"Оплата временно недоступна. Напиши {config.support_handle}."
            if config.support_handle
            else "Оплата временно недоступна. Попробуйте позже."
        )
        _, name, price, limits, _ = meta
        text = (
            f"<b>{name}</b>\n\n"
            f"💰 Стоимость: <b>{price}</b>\n"
            f"📦 Что входит: <b>{limits}</b>\n\n{contact}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К тарифам",    callback_data="menu:tariffs")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
        ])
        await _present(callback, text, keyboard=keyboard)
        await callback.answer()
        return

    # YooKassa requires customer.email for fiscal receipts under
    # Мой налог (мы — самозанятый). Ask once, save in DB, reuse on
    # subsequent purchases.
    email = await db.get_user_email(user_id)
    if not email:
        await state.set_state(BuyStates.waiting_for_email)
        await state.update_data(buy_tariff_id=tariff_id)
        text = (
            "📧 <b>Email для чека</b>\n\n"
            "Я самозанятый — на каждую оплату выпускается чек по 422-ФЗ. "
            "Пришли свой email сообщением, я сохраню его и больше не буду "
            "спрашивать.\n\n"
            "<i>/cancel — отмена</i>"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Отмена", callback_data="buy:cancel")],
        ])
        await _present(callback, text, keyboard=keyboard)
        await callback.answer()
        return

    await _start_yookassa_payment(callback, tariff_id, meta, rules, email=email)


async def _start_yookassa_payment(
    target, tariff_id: str, meta, rules: dict, *,
    email: str, label_prefix: str = "",
):
    """Create a YooKassa payment and reply with the «Оплатить» URL
    button. Activation happens later via the webhook (see webhook.py).

    `target` is either a CallbackQuery (when entering directly from
    the tariffs grid) or a Message (when entering after the email
    FSM step finished). `_present` handles both.
    """
    _, name, price, limits, _ = meta
    rub_amount = rules["kopeks"] / 100.0

    receipt_items = [yk.build_receipt_item(name, rub_amount)]
    metadata = {
        "telegram_id": str(target.from_user.id),
        "tariff_id": tariff_id,
    }
    bot = target.bot if hasattr(target, "bot") else None
    bot_username = ""
    if bot:
        try:
            me = await bot.me()
            bot_username = me.username or ""
        except Exception:
            pass
    return_url = (
        f"https://t.me/{bot_username}" if bot_username else "https://t.me"
    )

    try:
        payment = await yk.create_payment(
            shop_id=config.yookassa_shop_id,
            secret_key=config.yookassa_secret_key,
            amount_rub=rub_amount,
            description=f"{label_prefix}AutoSearch — {name}".strip(),
            metadata=metadata,
            return_url=return_url,
            receipt_items=receipt_items,
            customer_email=email,
        )
    except yk.YooKassaError:
        logger.exception("[payment] create_payment failed for tariff=%s", tariff_id)
        msg = "Не получилось открыть оплату. Попробуй чуть позже."
        if isinstance(target, CallbackQuery):
            await target.answer(msg, show_alert=True)
        else:
            await target.answer(msg)
        return

    confirmation_url = (payment.get("confirmation") or {}).get("confirmation_url")
    if not confirmation_url:
        logger.error("[payment] no confirmation_url in YooKassa response: %s", payment)
        msg = "ЮKassa не вернула ссылку на оплату — попробуй ещё раз."
        if isinstance(target, CallbackQuery):
            await target.answer(msg, show_alert=True)
        else:
            await target.answer(msg)
        return

    text = (
        f"<b>{name}</b>\n\n"
        f"💰 Стоимость: <b>{price}</b>\n"
        f"📦 Что входит: <b>{limits}</b>\n\n"
        f"Жми кнопку ниже — откроется страница ЮKassa с выбором способа "
        f"оплаты (карта, СБП, SberPay, ЮMoney). После оплаты тариф "
        f"активируется автоматически — вернись в Telegram."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {price}", url=confirmation_url)],
        [InlineKeyboardButton(text="⬅️ К тарифам",    callback_data="menu:tariffs")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
    ])
    await _present(target, text, keyboard=keyboard)
    if isinstance(target, CallbackQuery):
        await target.answer()


@router.callback_query(F.data == "buy:cancel")
async def callback_buy_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Отменено")
    await _show_tariffs(callback)


@router.message(Command("cancel"), StateFilter(BuyStates.waiting_for_email))
async def cmd_cancel_buy(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Отменено.", reply_markup=back_to_menu_keyboard())


@router.message(StateFilter(BuyStates.waiting_for_email), F.text)
async def handle_email_input(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not _EMAIL_RE.match(raw):
        await message.answer(
            "Это не похоже на email. Пришли в формате <code>name@example.com</code> "
            "или /cancel чтобы отмена.",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    tariff_id = data.get("buy_tariff_id")
    await state.clear()
    if not tariff_id:
        await message.answer(
            "Сессия покупки потерялась. Открой 💎 Тарифы заново.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    rules = _TARIFF_RULES.get(tariff_id)
    meta = _tariff_meta(tariff_id)
    if rules is None or meta is None:
        await message.answer("Неизвестный тариф.")
        return

    user_id = await db.get_or_create_user(
        message.from_user.id, message.from_user.username,
    )
    await db.set_user_email(user_id, raw)

    await _start_yookassa_payment(message, tariff_id, meta, rules, email=raw)


async def _show_add_hint(target):
    sources_pretty = ", ".join(source_display_name(s) for s in supported_sources())
    text = (
        "➕ <b>Добавить поиск</b>\n\n"
        "Открой нужный маркетплейс, настрой фильтры и пришли мне ссылку "
        "со страницы поиска.\n\n"
        f"<b>Поддерживаемые площадки:</b> <i>{sources_pretty}</i>\n\n"
        "Пример:\n"
        "<code>https://www.olx.pl/d/oferty/q-iphone-13/</code>"
    )
    await _present(target, text, keyboard=back_to_menu_keyboard())


async def _show_help(target):
    body = (
        "❓ <b>Как это работает</b>\n\n"
        "1. Открой нужный маркетплейс и настрой фильтры (бренд, размер, "
        "ценовой диапазон, регион).\n"
        "2. Скопируй URL страницы поиска и пришли его мне.\n"
        "3. Я буду каждую минуту проверять новые объявления и присылать их "
        "сюда — с фото, ценой в твоей валюте, локацией и описанием.\n\n"
        "<b>Команды:</b>\n"
        "/start — главное меню\n"
        "/list — мои поиски\n"
        "/profile — профиль и настройки\n"
        "/stop — приостановить все поиски\n"
        "/help — эта справка"
    )
    if config.support_handle:
        body += f"\n\n💬 <b>Поддержка:</b> {config.support_handle}"
    await _present(target, body, keyboard=back_to_menu_keyboard())


async def _show_profile(target, *, prefs: dict | None = None):
    """Profile screen — stats + language/currency change buttons.

    Settings (lang/currency) live here now, not on a separate Settings
    screen, so the user has one place for «everything about me»."""
    tg_id, username = _user_from(target)
    user_id = await db.get_or_create_user(tg_id, username)
    profile = await db.get_user_profile(user_id)
    if prefs is None:
        prefs = await db.get_user_prefs(user_id)

    user = profile["user"]
    reg_date = (
        user["created_at"].strftime("%d.%m.%Y")
        if user and user["created_at"] else "—"
    )
    last_found_str = "—"
    if profile["last_found"]:
        last_found_str = profile["last_found"]["sent_at"].strftime("%d.%m.%Y %H:%M")
    name = target.from_user.full_name or (user and user["username"]) or "Пользователь"

    # Resolve timezone: explicit pick first, fall back to a language-
    # derived default so existing rows (tz=NULL) still get a sensible
    # value on screen.
    tz = prefs.get("tz") or default_tz_for_lang(prefs.get("lang"))

    state = await _user_tariff_state(user_id, telegram_id=tg_id)
    if state["active"]:
        cur_meta = _tariff_meta(state["tariff_id"])
        cur_label = cur_meta[1] if cur_meta else state["tariff_id"]
        if state["days_left"] is not None:
            tariff_line = f"💎 Тариф: <b>{cur_label}</b> · до окончания ~{state['days_left']} дн."
        else:
            tariff_line = f"💎 Тариф: <b>{cur_label}</b>"
    else:
        tariff_line = "💎 Тариф: <b>не активирован</b> — открой 💎 Тарифы"

    text = (
        f"👤 <b>Профиль: {name}</b>\n\n"
        f"{tariff_line}\n"
        f"📊 Поисков: <b>{profile['active_subs']} / {state['max_subs']}</b>\n\n"
        f"📅 Регистрация: <b>{reg_date}</b>\n"
        f"📨 Объявлений найдено: <b>{profile['total_found']}</b>\n"
        f"🕐 Последнее найденное: <b>{last_found_str}</b>\n\n"
        f"⚙️ <b>Настройки</b>\n"
        f"🌐 Язык: <b>{language_label(prefs['lang'])}</b>\n"
        f"💱 Валюта: <b>{currency_label(prefs['currency'])}</b>\n"
        f"🕐 Часовой пояс: <b>{timezone_label(tz)}</b>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌐 Сменить язык",   callback_data="profile:lang"),
            InlineKeyboardButton(text="💱 Сменить валюту", callback_data="profile:cur"),
        ],
        [InlineKeyboardButton(text="🕐 Сменить часовой пояс", callback_data="profile:tz")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:home")],
    ])
    await _present(target, text, keyboard=keyboard)


async def _show_subscription_list(target):
    tg_id, username = _user_from(target)
    user_id = await db.get_or_create_user(tg_id, username)
    subs = await db.get_user_subscriptions(user_id)
    active = [s for s in subs if s["is_active"]]

    if not active:
        text = (
            "📋 <b>У тебя нет активных поисков.</b>\n\n"
            "Пришли ссылку на поиск с любого поддерживаемого маркетплейса — "
            "я начну мониторить."
        )
        await _present(target, text, keyboard=back_to_menu_keyboard())
        return

    lines = ["📋 <b>Активные поиски:</b>\n"]
    rename_buttons = []
    for i, sub in enumerate(active, 1):
        checked = sub["last_checked_at"]
        checked_str = (
            checked.strftime("%d.%m %H:%M") if checked else "ещё не проверялась"
        )
        errors = (
            f" ⚠️ ошибок: {sub['error_count']}" if sub["error_count"] > 0 else ""
        )
        name = _sub_display_name(sub)
        # Names + URLs come from the user / marketplace, escape before
        # inlining into HTML mode. Inline-button text is plain (Telegram
        # doesn't parse HTML there) so the name in callback button is
        # left raw.
        safe_name = _html.escape(name)
        safe_url = _html.escape(sub["url"], quote=True)
        lines.append(
            f"<b>{i}.</b> <a href=\"{safe_url}\">{safe_name}</a>\n"
            f"   Последняя проверка: {checked_str}{errors}"
        )
        rename_buttons.append([InlineKeyboardButton(
            text=f"✏️ Назвать «{name[:18]}»",
            callback_data=f"rename:{sub['id']}",
        )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        *rename_buttons,
        [InlineKeyboardButton(text="❌ Удалить поиск", callback_data="cmd:delete")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:home")],
    ])
    await _present(target, "\n".join(lines), keyboard=keyboard)


async def _show_delete_picker(target):
    tg_id, username = _user_from(target)
    user_id = await db.get_or_create_user(tg_id, username)
    subs = await db.get_user_subscriptions(user_id)
    active = [s for s in subs if s["is_active"]]

    if not active:
        await _present(
            target, "Нет активных поисков для удаления.",
            keyboard=back_to_menu_keyboard(),
        )
        return

    buttons = [
        [InlineKeyboardButton(
            text=f"❌ {_sub_display_name(sub)}"[:60],
            callback_data=f"del:{sub['id']}",
        )]
        for sub in active
    ]
    buttons.append([InlineKeyboardButton(
        text="⬅️ К списку поисков", callback_data="menu:list",
    )])
    await _present(
        target, "Выбери поиск для удаления:",
        keyboard=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# ---------------------------------------------------------------------------
# Slash commands — short-circuit straight to the relevant submenu
# ---------------------------------------------------------------------------

@router.message(Command("profile"))
async def cmd_profile(message: Message):
    await _show_profile(message)


@router.message(Command("settings"))
async def cmd_settings(message: Message):
    # Settings folded into Profile — point the user there.
    await _show_profile(message)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await _show_help(message)


@router.message(Command("list"))
async def cmd_list(message: Message):
    await _show_subscription_list(message)


@router.message(Command("delete"))
async def cmd_delete(message: Message):
    await _show_delete_picker(message)


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    """Multi-tab admin panel.

    Usage:
      /admin              — dashboard (revenue + paid users + traffic)
      /admin users        — latest 30 users with paid/active tariffs
      /admin user <tg_id> — drill-down: profile + subs + payments
    """
    if not is_admin(message.from_user.id):
        return

    raw = (message.text or "").strip().split()
    if len(raw) >= 2 and raw[1] == "users":
        await _admin_users_list(message)
        return
    if len(raw) >= 3 and raw[1] == "user":
        try:
            target_tg = int(raw[2])
        except ValueError:
            await message.answer("Использование: /admin user &lt;telegram_id&gt;")
            return
        await _admin_user_detail(message, target_tg)
        return

    await _admin_dashboard(message)


async def _admin_dashboard(message: Message):
    stats = await db.get_admin_stats()

    last_checked_str = "—"
    if stats.get("last_checked"):
        from datetime import timedelta, timezone as tz
        msk = tz(timedelta(hours=3))
        last_checked_str = stats["last_checked"].astimezone(msk).strftime(
            "%H:%M %d.%m.%Y"
        )

    rev_total = stats.get("revenue_total", 0) // 100  # kopeks → rubles
    rev_30d = stats.get("revenue_30d", 0) // 100

    await message.answer(
        f"📊 <b>Админ-панель</b>\n\n"
        f"<b>💰 Выручка</b>\n"
        f"  За всё время: <b>{rev_total:,} ₽</b>\n"
        f"  За 30 дней:   <b>{rev_30d:,} ₽</b>\n"
        f"  Платящих сейчас: <b>{stats.get('active_paid_users', 0):,}</b>\n"
        f"  Уникальных платежей: <b>{stats.get('paid_users_total', 0):,}</b> "
        f"(за 30д: {stats.get('paid_users_30d', 0):,})\n\n"
        f"<b>👥 Юзеры</b>\n"
        f"  Всего: <b>{stats['total_users']:,}</b>\n"
        f"  Новых за 24ч: <b>{stats['new_users_24h']:,}</b>\n\n"
        f"<b>🔍 Поиски</b>\n"
        f"  Активных: <b>{stats['active_subs']:,}</b>\n"
        f"  Уникальных URL: <b>{stats['unique_urls']:,}</b>\n"
        f"  Уведомлений всего: <b>{stats['total_sent']:,}</b>\n"
        f"  За 24ч: <b>{stats['sent_24h']:,}</b>\n"
        f"  Последняя проверка: <b>{last_checked_str}</b>\n\n"
        f"<i>/admin users — список платящих\n"
        f"/admin user &lt;tg_id&gt; — детали юзера</i>",
        parse_mode="HTML",
    )


async def _admin_users_list(message: Message):
    rows = await db.get_admin_user_list(limit=30)
    if not rows:
        await message.answer("Платящих юзеров пока нет.")
        return

    lines = ["📋 <b>Платящие юзеры (top 30)</b>\n"]
    for r in rows:
        tariff_meta = _tariff_meta(r["tariff"])
        tariff_label = tariff_meta[1] if tariff_meta else (r["tariff"] or "—")
        # days left
        exp = r.get("tariff_expires_at")
        if exp:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            if exp > now:
                days = max(1, int((exp - now).total_seconds() // 86400))
                exp_str = f"{days}д"
            else:
                exp_str = "истёк"
        else:
            exp_str = "∞" if r["tariff"] == "legacy" else "—"
        username = r.get("username") or "—"
        lifetime = (r.get("lifetime_paid") or 0) // 100
        lines.append(
            f"<code>{r['telegram_id']}</code> @{_html.escape(username)} · "
            f"{tariff_label} · {exp_str} · "
            f"📋{r['active_subs']} · 💰{lifetime:,}₽"
        )
    lines.append("\n<i>/admin user &lt;tg_id&gt; — детали</i>")
    await message.answer("\n".join(lines), parse_mode="HTML")


async def _admin_user_detail(message: Message, target_tg: int):
    detail = await db.get_admin_user_detail(target_tg)
    if detail is None:
        await message.answer(
            f"Юзер с tg_id <code>{target_tg}</code> не найден.",
            parse_mode="HTML",
        )
        return

    u = detail["user"]
    tariff_meta = _tariff_meta(u["tariff"])
    tariff_label = tariff_meta[1] if tariff_meta else (u["tariff"] or "—")
    reg_str = u["created_at"].strftime("%d.%m.%Y") if u.get("created_at") else "—"
    exp_str = (
        u["tariff_expires_at"].strftime("%d.%m.%Y %H:%M UTC")
        if u.get("tariff_expires_at") else "—"
    )

    safe_username = _html.escape(u.get("username") or "—")
    lines = [
        f"👤 <b>@{safe_username}</b> "
        f"(<code>{u['telegram_id']}</code>)",
        f"📅 Регистрация: <b>{reg_str}</b>",
        f"💎 Тариф: <b>{tariff_label}</b> до {exp_str}",
        f"🎁 Trial used: <b>{'да' if u.get('trial_used') else 'нет'}</b>",
        "",
    ]

    subs = detail["subs"]
    if subs:
        lines.append(f"<b>📋 Поиски ({len(subs)}):</b>")
        for s in subs[:10]:
            name = s.get("name") or source_display_name(s.get("source") or "")
            status = "🟢" if s["is_active"] else "⏸"
            errs = f" ⚠️{s['error_count']}" if (s.get("error_count") or 0) > 0 else ""
            lines.append(
                f"  {status} #{s['id']} {_html.escape(name)} "
                f"({s.get('source') or '—'}){errs}"
            )
        if len(subs) > 10:
            lines.append(f"  <i>… и ещё {len(subs) - 10}</i>")
        lines.append("")

    payments = detail["payments"]
    if payments:
        total = sum(p.get("amount_minor", 0) for p in payments) // 100
        lines.append(f"<b>💰 Платежи ({len(payments)}, всего {total:,} ₽):</b>")
        for p in payments[:10]:
            amt = (p.get("amount_minor", 0)) // 100
            when = p["created_at"].strftime("%d.%m.%Y") if p.get("created_at") else "—"
            lines.append(
                f"  {when} — {p.get('tariff_id', '?')}: "
                f"{amt:,} {p.get('currency', '')}"
            )
        if len(payments) > 10:
            lines.append(f"  <i>… и ещё {len(payments) - 10}</i>")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("testbuy"))
async def cmd_testbuy(message: Message):
    """Admin-only: trigger the real payment flow for a given tariff.

    Bypasses the «admins don't pay» short-circuit on the inline buy
    buttons (which is there to protect against accidental clicks
    during demos). Use this command deliberately to verify YooKassa
    integration end-to-end with a real card / SBP.

    Usage: /testbuy <tariff_id>
      tariff_id ∈ trial / basic / advanced / pro

    The successful_payment handler will record the payment and write
    the tariff to DB, but the admin still resolves to 'admin' tariff
    at runtime via is_admin() — so admin status is not affected.
    """
    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        await message.answer(
            "Использование: <code>/testbuy &lt;tariff_id&gt;</code>\n\n"
            "<b>Доступные:</b>\n"
            "• <code>/testbuy trial</code> — 0 ₽ (один раз на юзера)\n"
            "• <code>/testbuy basic</code> — 890 ₽\n"
            "• <code>/testbuy advanced</code> — 1 790 ₽\n"
            "• <code>/testbuy pro</code> — 2 590 ₽\n\n"
            "<i>Реальная оплата (или возврат через YooKassa-дашборд "
            "после теста). Админ-статус не меняется — у тебя останется "
            "безлимит независимо от платежа.</i>",
            parse_mode="HTML",
        )
        return

    tariff_id = parts[1].lower()
    rules = _TARIFF_RULES.get(tariff_id)
    if rules is None or tariff_id in ("legacy", "admin"):
        await message.answer(
            f"Неизвестный тариф: <code>{_html.escape(tariff_id)}</code>",
            parse_mode="HTML",
        )
        return

    user_id = await db.get_or_create_user(
        message.from_user.id, message.from_user.username,
    )
    meta = _tariff_meta(tariff_id)
    name = meta[1] if meta else tariff_id

    # Trial — same atomic activation as the normal flow.
    if tariff_id == "trial":
        new_exp = await db.activate_tariff(
            user_id, "trial", rules["hours"], is_trial=True,
        )
        if new_exp is None:
            await message.answer(
                "Trial у этого аккаунта уже был активирован. "
                "Тестируй платный тариф или сбрось <code>trial_used</code> "
                "вручную в БД.",
                parse_mode="HTML",
            )
            return
        await message.answer(
            f"✅ Trial активирован для теста ({name}). "
            f"Админ-статус не изменился."
        )
        return

    if not (config.yookassa_shop_id and config.yookassa_secret_key
            and config.webhook_base_url):
        await message.answer(
            "YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY / WEBHOOK_BASE_URL "
            "не настроены — оплата не запустится."
        )
        return

    # Email needed for the receipt under Мой налог. Reuse the value
    # the admin already saved (likely from a previous /testbuy run);
    # fall back to a hardcoded test address otherwise so the admin
    # doesn't have to type it on every iteration.
    email = await db.get_user_email(user_id) or "test@example.com"

    rub_amount = rules["kopeks"] / 100.0
    receipt_items = [yk.build_receipt_item(name, rub_amount)]
    metadata = {
        "telegram_id": str(message.from_user.id),
        "tariff_id": tariff_id,
    }
    bot_username = (await message.bot.me()).username
    return_url = f"https://t.me/{bot_username}" if bot_username else "https://t.me"

    try:
        payment = await yk.create_payment(
            shop_id=config.yookassa_shop_id,
            secret_key=config.yookassa_secret_key,
            amount_rub=rub_amount,
            description=f"[TEST] AutoSearch — {name}",
            metadata=metadata,
            return_url=return_url,
            receipt_items=receipt_items,
            customer_email=email,
        )
    except yk.YooKassaError as e:
        logger.exception("[testbuy] create_payment failed for tariff=%s", tariff_id)
        await message.answer(f"YooKassa error: {e}")
        return

    confirmation_url = (payment.get("confirmation") or {}).get("confirmation_url")
    if not confirmation_url:
        logger.error("[testbuy] no confirmation_url: %s", payment)
        await message.answer("ЮKassa не вернула ссылку — гляну логи.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {meta[2]}", url=confirmation_url)],
    ])
    await message.answer(
        f"🧪 <b>Тест YooKassa — {name}</b>\n\n"
        f"Сумма: <b>{rub_amount:.2f} ₽</b>\n"
        f"Email чека: <code>{_html.escape(email)}</code>\n"
        f"Payment ID: <code>{payment.get('id', '?')}</code>\n\n"
        f"Жми кнопку → откроется страница ЮKassa со всеми способами "
        f"оплаты. Админ-статус не изменится после оплаты.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


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
        reply_markup=back_to_menu_keyboard(),
    )


@router.callback_query(F.data == "cmd:delete")
async def callback_cmd_delete(callback: CallbackQuery):
    await _show_delete_picker(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("del:"))
async def callback_delete(callback: CallbackQuery):
    try:
        sub_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID")
        return
    await db.deactivate_subscription(sub_id)
    await callback.answer("Удалено")
    # Refresh the list — the deleted item disappears in place.
    await _show_subscription_list(callback)


# ---------------------------------------------------------------------------
# Rename flow (FSM)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("rename:"))
async def callback_rename(callback: CallbackQuery, state: FSMContext):
    try:
        sub_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID")
        return
    user_id = await db.get_or_create_user(
        callback.from_user.id, callback.from_user.username,
    )
    sub = await db.get_subscription_owned_by(sub_id, user_id)
    if sub is None:
        await callback.answer("Поиск не найден", show_alert=True)
        return

    current = _sub_display_name(dict(sub))
    await state.set_state(RenameStates.waiting_for_name)
    await state.update_data(rename_sub_id=sub_id)

    text = (
        f"✏️ <b>Новое название</b>\n\n"
        f"Сейчас: <b>{_html.escape(current)}</b>\n\n"
        f"Пришли новое название следующим сообщением (до {_MAX_NAME_LEN} символов).\n"
        f"<i>/cancel — отмена</i>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Отмена", callback_data="rename:cancel")],
    ])
    await _present(callback, text, keyboard=keyboard)
    await callback.answer()


@router.callback_query(F.data == "rename:cancel")
async def callback_rename_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Отменено")
    await _show_subscription_list(callback)


@router.message(Command("cancel"), StateFilter(RenameStates.waiting_for_name))
async def cmd_cancel_rename(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Отменено.", reply_markup=back_to_menu_keyboard())


@router.message(StateFilter(RenameStates.waiting_for_name), F.text)
async def handle_rename_input(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw:
        await message.answer(
            "Название не может быть пустым. Пришли текст или /cancel.",
        )
        return
    if len(raw) > _MAX_NAME_LEN:
        raw = raw[:_MAX_NAME_LEN]

    data = await state.get_data()
    sub_id = data.get("rename_sub_id")
    await state.clear()
    if sub_id is None:
        await message.answer("Что-то пошло не так. Попробуй ещё раз через 📋 «Мои поиски».")
        return

    user_id = await db.get_or_create_user(
        message.from_user.id, message.from_user.username,
    )
    ok = await db.set_subscription_name(int(sub_id), user_id, raw)
    if not ok:
        await message.answer(
            "Поиск не найден или уже удалён.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    await message.answer(
        f"✅ Поиск переименован в <b>{_html.escape(raw)}</b>.",
        parse_mode="HTML",
    )
    await _show_subscription_list(message)


# ---------------------------------------------------------------------------
# Free-text URL handler — main "add subscription" entry point
# ---------------------------------------------------------------------------

@router.message(F.text, StateFilter(default_state))
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

    state = await _user_tariff_state(user_id, telegram_id=message.from_user.id)
    if not state["active"]:
        # No active tariff at all — gate behind the paywall.
        await message.answer(
            "🔒 <b>Чтобы добавить поиск, нужен тариф.</b>\n\n"
            "Активируй <b>🎁 Пробный</b> на 6 часов бесплатно или выбери "
            "платный — открывай 💎 <b>Тарифы</b>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Тарифы", callback_data="menu:tariffs")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
            ]),
        )
        return

    sub_id = await db.add_subscription(
        user_id, url, source=source_name,
        max_subscriptions=state["max_subs"],
    )
    if sub_id is None:
        await message.answer(
            f"⚠️ Достигнут лимит твоего тарифа — максимум "
            f"{state['max_subs']} {'поиск' if state['max_subs'] == 1 else 'поисков'}.\n\n"
            "Удали ненужные через 📋 «Мои поиски» → ❌ Удалить, или "
            "перейди на тариф повыше в 💎 Тарифы.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Тарифы", callback_data="menu:tariffs")],
                [InlineKeyboardButton(text="📋 Мои поиски", callback_data="menu:list")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
            ]),
        )
        return

    pretty = source_display_name(source_name)
    await message.answer(
        f"⏳ <b>Добавляю поиск {pretty}</b>\n"
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

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✏️ Назвать поиск", callback_data=f"rename:{sub_id}",
        )],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
    ])
    await message.answer(
        f"✅ <b>Мониторинг запущен</b> ({pretty})\n\n"
        f"🔗 <a href=\"{url}\">Твоя ссылка</a>\n\n"
        f"Записал {seeded} текущих объявлений как уже виденные. "
        f"Как появится новое — пришлю с фото, ценой и описанием.\n\n"
        f"<i>Хочешь дать поиску своё название? Жми ✏️</i>",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )
