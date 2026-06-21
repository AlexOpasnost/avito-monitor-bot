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
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from datetime import datetime, timedelta, timezone

from services import yookassa as yk

from bot_i18n import (
    LANGUAGE_CODES, CURRENCY_CODES, TIMEZONE_CODES,
    language_label, currency_label, timezone_label,
    language_keyboard, currency_keyboard, timezone_keyboard,
    main_menu_keyboard, back_to_menu_keyboard,
    default_tz_for_lang,
)
from config import config
from database import db, UserTombstonedError
from parser import detect_source, fetch_search_items, supported_sources
from parsers import is_source_disabled, source_display_name
from parsers.common import proxy_for_source

logger = logging.getLogger(__name__)
router = Router()

# DM-only enforcement. Without this, a user can add the bot to a group
# chat and run /export_my_data, /list, or paste a URL in front of every
# group member — the bot has no group features and the privacy
# implications of leaking subscription/email/JSON dumps into a shared
# room are real. Centralising the check here covers ALL message handlers
# (commands, FSM input, free-text URL paste) in one line; the previous
# inline `if chat.type != "private": return` blocks become redundant.
router.message.filter(F.chat.type == "private")


# Erasure-correctness backstop. `db.get_or_create_user` raises
# UserTombstonedError when the telegram_id has previously requested
# /delete_my_account. Every command, callback, and FSM input step
# touches that helper at some point, so registering one global
# error-handler at the router level is cheaper and harder to drift
# than wrapping every callsite individually. Without this, a
# tombstoned user typing /start sees a 500 (asyncpg surfaces the
# raise as a polling-loop traceback) instead of the polite
# "account deleted" response the privacy policy promises.
@router.errors()
async def on_user_tombstoned(event: ErrorEvent):
    exc = event.exception
    if not isinstance(exc, UserTombstonedError):
        # Surface the exception type/message into the log so we don't
        # have a black hole when something OTHER than tombstone raises
        # inside a handler. aiogram's default logger prints it too,
        # but only at ERROR — making this our second canary if Sentry
        # routing changes.
        logger.error(
            "[errors] non-tombstone exception in handler: %s: %s",
            type(exc).__name__, exc, exc_info=exc,
        )
        return False
    update = event.update
    target = None
    if getattr(update, "message", None) is not None:
        target = update.message
    elif getattr(update, "edited_message", None) is not None:
        target = update.edited_message
    elif getattr(update, "callback_query", None) is not None:
        target = update.callback_query.message
    if target is None:
        # Nothing to reply to (rare update types). Still mark the
        # error as handled so it doesn't propagate to the polling loop.
        return True
    text = (
        "🗂 <b>Аккаунт удалён</b>\n\n"
        "Ты использовал /delete_my_account, и твои данные стёрты "
        "по твоему запросу (152-ФЗ Art. 14). Восстановить их нельзя.\n\n"
        "Если ты передумал и хочешь начать заново с чистого листа — "
        "напиши в поддержку, и мы снимем технический tombstone "
        "(после этого можно будет /start)."
    )
    if config.support_handle:
        text += f"\n\n💬 Поддержка: {config.support_handle}"
    tg_id = getattr(getattr(target, "from_user", None), "id", None)
    logger.info("[tombstone] showing screen to tg_id=%s", tg_id)
    try:
        await target.answer(text, parse_mode="HTML")
        logger.info("[tombstone] reply sent ok to tg_id=%s", tg_id)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        # User blocked the bot or the message is somehow malformed —
        # nothing else we can do. Don't re-raise.
        logger.warning(
            "[tombstone] reply failed for tg_id=%s: %s", tg_id, e,
        )
    except Exception:
        logger.exception("[tombstone] reply unexpected fail tg_id=%s", tg_id)
    return True


_GENERIC_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _word_hash(word: str) -> str:
    """8-hex-char content hash of a stop-word, for callback_data keying.

    Index-based callback keys (`bl_del:&lt;sub_id&gt;:&lt;idx&gt;`) race with rapid
    double-tap: tap 1 pops idx=2, the list shifts, tap 2 (still the
    OLD idx=3) now hits a different word. Content hashing decouples
    button identity from list position.

    md5 truncated to 8 chars: 32 bits of space, low collision risk
    inside a sub's stop-list (capped at 50 words). Not a security
    primitive — just a stable identifier for callback routing.
    """
    import hashlib
    return hashlib.md5(word.encode("utf-8")).hexdigest()[:8]

# Max length of a user-set subscription label. Telegram caption /
# inline-button width get unhappy with anything much longer.
_MAX_NAME_LEN = 30


class RenameStates(StatesGroup):
    """FSM: user clicked ✏️, the next text message they send becomes
    the subscription's new name."""
    waiting_for_name = State()


class BlacklistStates(StatesGroup):
    """FSM: user clicked ➕ on the stop-words screen, the next text
    message contains comma- or newline-separated words to add."""
    waiting_for_words = State()


class BuyStates(StatesGroup):
    """FSM: user clicked a paid tariff button, we asked for their
    email (required for the YooKassa receipt under Мой налог), the
    next text message they send is the email."""
    waiting_for_email = State()


# Tightened email check. YooKassa validates the format properly when
# issuing the receipt, but we want to reject obvious mistypes AND
# anything that looks like an injection payload before storing in DB
# / logging / sending to YooKassa. The previous regex (`[^\s@]+@…`)
# accepted `<script>@x.co`, `"foo"@bar.co`, and any value with `<`,
# `>`, `'`, `"`, backslashes, control chars — none of those are
# producible by a real email address per RFC 5321 unprefixed.
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,253}\.[A-Za-z]{2,24}$"
)


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

    Wraps the outermost send in try/except that surfaces failures into
    the Railway log via `logger.exception`. Without this, a broken
    Telegram-API endpoint / forbidden / parse-mode error from the
    underlying `target.answer` is invisible: aiogram's dispatcher
    still prints `Update id=… is handled. Duration N ms` even when
    the handler raised — making "handler ran, user sees nothing"
    bugs un-debuggable. See ROUND 14 incident.
    """
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(
                text, parse_mode="HTML", reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return
        except TelegramBadRequest:
            try:
                await target.message.answer(
                    text, parse_mode="HTML", reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception(
                    "[_present] cb-fallback answer failed (text head=%r)",
                    text[:80],
                )
            return
    try:
        await target.answer(
            text, parse_mode="HTML", reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception:
        logger.exception(
            "[_present] answer failed (text head=%r)", text[:80],
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
               "﻿", " ", "", ""):
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


async def _warn_if_url_during_fsm(
    message: Message, expected: str,
) -> bool:
    """Detect a URL pasted while waiting for FSM input and bail.

    A user who's mid-flow (e.g. on the «введи email для чека» step) and
    pastes ANY URL almost certainly meant to add a new search, not to
    set the URL as their email or sub name. Without this guard we
    silently store «https://avito.ru/...» as the user's email and the
    next YooKassa create-payment fails with "invalid email".

    Catches BOTH supported-marketplace URLs (the obvious case) AND
    generic http(s) URLs — a user who pastes `https://evil.tld/...` on
    the email step would otherwise have it stored verbatim and rendered
    as a clickable label later.

    Returns True if a warning was sent — the caller should return
    immediately and leave the FSM state untouched so /cancel still works.
    """
    text = (message.text or message.caption or "").strip()
    if (_extract_marketplace_url(message) is None
            and not _GENERIC_URL_RE.search(text)):
        return False
    try:
        await message.answer(
            f"Сейчас я жду {expected}, а ты прислал ссылку. Если хочешь "
            f"добавить новый поиск — отправь /cancel и пришли ссылку ещё раз.",
        )
    except Exception:
        # Same silent-fail family as ROUND 14 incident — without the
        # try/except a TelegramForbidden / network blip vanishes here
        # and the user is stuck in an FSM with no visible warning.
        logger.exception(
            "[fsm-warn] answer failed tg_id=%s expected=%r",
            message.from_user.id, expected,
        )
    return True


# ---------------------------------------------------------------------------
# Onboarding wizard + main menu
# ---------------------------------------------------------------------------

def _legal_footer() -> str:
    """Build the «нажимая Поехали соглашаешься...» line shown in the
    onboarding hero. We only mention the documents that are actually
    published — empty config values drop the link cleanly."""
    parts = []
    if config.offer_url:
        parts.append(f"<a href=\"{_html.escape(config.offer_url)}\">офертой</a>")
    if config.privacy_url:
        parts.append(
            f"<a href=\"{_html.escape(config.privacy_url)}\">"
            "политикой обработки ПД</a>"
        )
    if not parts:
        return ""
    if len(parts) == 1:
        return f"\n\n<i>Нажимая «🚀 Поехали», соглашаешься с {parts[0]}.</i>"
    return (
        f"\n\n<i>Нажимая «🚀 Поехали», соглашаешься с "
        f"{parts[0]} и {parts[1]}.</i>"
    )


_HERO_TEXT_BASE = (
    "👋 <b>Добро пожаловать в AutoSearch!</b>\n\n"
    "Я мониторю Avito, Юла, OLX, Vinted, Kufar, Mercari, Grailed, Fruitsfamily и другие площадки и "
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
        target, _HERO_TEXT_BASE + _legal_footer(),
        keyboard=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚀 Поехали", callback_data="onboard:lang"),
        ]]),
    )


# ---------------------------------------------------------------------------
# Channel-subscribe gate
# ---------------------------------------------------------------------------
# Gate shown on /start when REQUIRED_CHANNEL env var is set. The bot
# must be an admin in the channel for getChatMember to succeed for
# every user — otherwise Telegram replies "member list is inaccessible"
# and we fail open (let the user through with a logged warning) so a
# misconfigured channel doesn't lock the entire userbase out of the bot.

_GATE_TEXT = (
    "📢 <b>Подпишись на канал</b>\n\n"
    "Для пользования ботом подпишись на наш канал: {handle}\n"
    "👉 Все новости про проект будут там."
)


def _channel_public_url(handle: str) -> str | None:
    """`@autoserch` → `https://t.me/autoserch`. Numeric `-100…` ids
    have no public URL — return None and the gate will only render the
    «Я подписался» button."""
    h = (handle or "").strip()
    if not h:
        return None
    if h.startswith("@"):
        slug = h[1:]
        return f"https://t.me/{slug}" if slug else None
    if h.startswith(("https://t.me/", "http://t.me/")):
        return h
    if h.startswith("t.me/"):
        return f"https://{h}"
    return None


async def _is_subscribed_to_required_channel(bot, telegram_id: int) -> bool:
    """True if user joined config.required_channel, OR the gate is
    disabled, OR Telegram couldn't tell us (fail-open).

    Status semantics: "creator" | "administrator" | "member" pass;
    "left" | "kicked" | "restricted" fail. Any TelegramBadRequest /
    TelegramForbiddenError (channel not found, bot not admin, member
    list inaccessible) fails open with a warning — locking everyone
    out because of a misconfigured channel is worse than letting a
    handful of users skip the gate."""
    channel = (config.required_channel or "").strip()
    if not channel:
        return True
    if is_admin(telegram_id):
        return True
    try:
        member = await bot.get_chat_member(channel, telegram_id)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning(
            "[channel-gate] get_chat_member failed for %s in %s: %s",
            telegram_id, channel, e,
        )
        return True
    status = getattr(member, "status", None)
    return status in ("creator", "administrator", "member")


async def _show_channel_gate(target):
    handle = (config.required_channel or "").strip()
    text = _GATE_TEXT.format(handle=_html.escape(handle))
    rows = []
    url = _channel_public_url(handle)
    if url:
        rows.append([InlineKeyboardButton(text="📢 Открыть канал", url=url)])
    rows.append([InlineKeyboardButton(
        text="✅ Я подписался", callback_data="gate:check",
    )])
    await _present(
        target, text,
        keyboard=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _channel_gate_passes(target) -> bool:
    """Return True if the user passed the gate (or the gate is off /
    failed open). Return False AND show the gate screen otherwise —
    caller should bail without rendering its own UI."""
    bot = getattr(target, "bot", None)
    if bot is None:
        # No bot reference — typically only happens in unit tests with
        # synthetic targets. Don't gate.
        return True
    if await _is_subscribed_to_required_channel(bot, target.from_user.id):
        return True
    await _show_channel_gate(target)
    return False


# ---------------------------------------------------------------------------
# Re-consent on policy version bump
# ---------------------------------------------------------------------------
# When PRIVACY/OFFER are changed substantively (new processing purpose,
# new third party, materially different commercial terms), 152-ФЗ Art. 9
# ч.4 requires a fresh affirmative consent from EXISTING users. New users
# coming through onboarding stamp themselves with the current version
# automatically (set_user_onboarded). Existing users need this screen.
#
# Trigger: prefs.onboarded == True AND prefs.consent_policy_version
# differs from config.consent_policy_version. NULL counts as different
# (legacy users registered before consent-capture was wired up).

_RECONSENT_TEXT = (
    "⚠️ <b>Условия использования обновлены</b>\n\n"
    "С твоего последнего захода мы обновили публичную оферту и политику "
    "обработки персональных данных до версии <b>v3 (02.05.2026)</b>.\n\n"
    "<b>Что изменилось:</b>\n\n"
    "1. <b>Подписка на канал @autoserch</b> — теперь условие доступа "
    "к боту. Бот проверяет факт подписки через Telegram API при /start.\n\n"
    "2. <b>Цены тарифов</b> — Базовый 1 290 ₽, Продвинутый 1 990 ₽, "
    "Профессиональный 2 990 ₽. Уже оплаченные подписки изменения "
    "не затрагивают.\n\n"
    "3. <b>Условия возврата переписаны</b> — пропорциональный возврат "
    "по ст. 32 ЗоЗПП. Теперь возврат можно получить и после первого "
    "уведомления, пропорционально неиспользованным дням срока тарифа.\n\n"
    "Для продолжения использования бота необходимо подтвердить согласие "
    "с новой редакцией. Если не согласен — ты в любой момент можешь "
    "выгрузить свои данные (/export_my_data) и удалить аккаунт "
    "(/delete_my_account)."
)


def _reconsent_keyboard() -> InlineKeyboardMarkup:
    rows = []
    legal = []
    if config.offer_url:
        legal.append(InlineKeyboardButton(
            text="📄 Оферта", url=config.offer_url,
        ))
    if config.privacy_url:
        legal.append(InlineKeyboardButton(
            text="🔒 Политика", url=config.privacy_url,
        ))
    if legal:
        rows.append(legal)
    rows.append([InlineKeyboardButton(
        text="✅ Принимаю новую редакцию", callback_data="reconsent:accept",
    )])
    rows.append([InlineKeyboardButton(
        text="❌ Не принимаю", callback_data="reconsent:decline",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_reconsent(target):
    await _present(target, _RECONSENT_TEXT, keyboard=_reconsent_keyboard())


async def _reconsent_passes(target, prefs: dict) -> bool:
    """Return True when the user is good (admin, OR new user, OR
    consent matches current version). Return False AND show the
    re-consent screen for existing onboarded users on a stale policy
    version. Caller should bail without rendering its own UI."""
    tg_id = getattr(getattr(target, "from_user", None), "id", None)
    if tg_id and is_admin(tg_id):
        # Admins bypass re-consent: they're internal operators, not
        # end-users whose consent the operator needs to record. Without
        # this bypass the admin gets stuck on the re-consent screen on
        # every /start during version bumps and can't smoke-test the
        # rest of the flow.
        return True
    if not prefs.get("onboarded"):
        # New user — onboarding will stamp the current version when
        # they finish (set_user_onboarded with policy_version=…).
        return True
    current = (config.consent_policy_version or "").strip()
    if not current:
        # Operator hasn't set a version — don't force re-consent on
        # an unconfigured policy_version. Belt-and-suspenders for
        # local dev where the env var might be empty.
        return True
    stored = (prefs.get("consent_policy_version") or "").strip()
    if stored == current:
        return True
    logger.info(
        "[reconsent] showing screen to tg_id=%s (stored=%r current=%r)",
        tg_id, stored, current,
    )
    await _show_reconsent(target)
    return False


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


@router.message(Command("ping"))
async def cmd_ping(message: Message):
    """Diagnostic-only handler. Replies «pong» with zero DB / API
    dependencies — used to isolate whether `bot.send_message` works at
    all, vs. `cmd_start` having a logic bug. If `/ping` answers but
    `/start` doesn't → bug is in cmd_start. If `/ping` is also silent
    → it's a Telegram-API / shadow-ban / token issue, not our code."""
    logger.info("[ping] received from tg_id=%s", message.from_user.id)
    try:
        await message.answer("pong")
        logger.info("[ping] reply sent ok to tg_id=%s", message.from_user.id)
    except Exception:
        logger.exception("[ping] reply failed to tg_id=%s", message.from_user.id)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    logger.info("[start] entered tg_id=%s admin=%s", tg_id, is_admin(tg_id))
    # Hard reset on every /start: a user typing /start to escape a
    # broken FSM state (mid-purchase email prompt, mid-rename input,
    # mid-blacklist edit) must ALWAYS land back at the main menu —
    # not get re-funnelled into the FSM step they were stuck in.
    # `state.clear()` drops any current state + payload before we do
    # anything else. Cheap (in-memory by default) and always-safe.
    await state.clear()
    user_id = await db.get_or_create_user(
        tg_id, message.from_user.username,
    )
    prefs = await db.get_user_prefs(user_id)
    logger.info(
        "[start] prefs tg_id=%s onboarded=%s consent=%r",
        tg_id, prefs.get("onboarded"),
        prefs.get("consent_policy_version"),
    )

    # Re-consent gate: existing users on an older policy version must
    # explicitly accept the current one before continuing. New users
    # (onboarded=False) skip this — the onboarding wizard stamps them
    # with the current version on completion.
    if not await _reconsent_passes(message, prefs):
        logger.info("[start] bailed at reconsent gate tg_id=%s", tg_id)
        return

    # Reactivate paused subs ONLY if the user still has the tariff to
    # back them. Admins also bypass — they always have access.
    if is_admin(tg_id) or await db.has_active_tariff(tg_id):
        await db.reactivate_all(user_id)

    # Channel-subscribe gate was removed from cmd_start by operator
    # request (2026-06-19): forcing every user through a channel-join
    # screen hurt onboarding more than it helped distribution. The
    # gate plumbing (_channel_gate_passes, callback_gate_check,
    # config.required_channel) stays in place — if the operator
    # later wants to bring it back, setting REQUIRED_CHANNEL in env
    # and re-adding the guard here is a single-line revert.

    logger.info(
        "[start] rendering %s tg_id=%s",
        "main_menu" if prefs.get("onboarded") else "hero", tg_id,
    )
    if prefs.get("onboarded"):
        await _show_main_menu(message, prefs)
    else:
        await _show_hero(message)
    logger.info("[start] rendered ok tg_id=%s", tg_id)


@router.callback_query(F.data == "reconsent:accept")
async def callback_reconsent_accept(callback: CallbackQuery):
    user_id = await db.get_or_create_user(
        callback.from_user.id, callback.from_user.username,
    )
    await db.record_reconsent(user_id, config.consent_policy_version)
    await callback.answer("✅ Спасибо! Согласие записано.")
    # Continue forward into the channel gate (or hero/menu if disabled
    # or already passed) using the same flow as /start.
    if not await _channel_gate_passes(callback):
        return
    prefs = await db.get_user_prefs(user_id)
    if prefs.get("onboarded"):
        await _show_main_menu(callback, prefs)
    else:
        await _show_hero(callback)


@router.callback_query(F.data == "reconsent:decline")
async def callback_reconsent_decline(callback: CallbackQuery):
    text = (
        "🚫 <b>Согласие не получено</b>\n\n"
        "Без согласия с новой редакцией оферты и политики обработки "
        "персональных данных продолжение работы с ботом невозможно. "
        "Твои данные пока остаются на месте — ты можешь:\n\n"
        "• <b>/export_my_data</b> — выгрузить копию своих данных в JSON\n"
        "• <b>/delete_my_account</b> — удалить аккаунт и все данные "
        "(152-ФЗ ст. 14)\n"
        "• <b>/start</b> — пересмотреть условия и принять, если передумаешь"
    )
    if config.support_handle:
        text += f"\n\n💬 Вопросы по новым условиям: {config.support_handle}"
    # No «Главное меню» button — that would let them bypass re-consent.
    # Only path forward is to re-read the terms (and accept) or
    # /delete_my_account.
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🔄 Пересмотреть условия", callback_data="reconsent:show",
        ),
    ]])
    await _present(callback, text, keyboard=keyboard)
    await callback.answer()


@router.callback_query(F.data == "reconsent:show")
async def callback_reconsent_show(callback: CallbackQuery):
    """Re-display the re-consent screen (entry from the decline-confirmation
    screen, in case the user changes their mind without doing /start)."""
    await _show_reconsent(callback)
    await callback.answer()


@router.callback_query(F.data == "gate:check")
async def callback_gate_check(callback: CallbackQuery):
    if not await _is_subscribed_to_required_channel(
        callback.bot, callback.from_user.id,
    ):
        await callback.answer(
            "Подписка не найдена — открой канал, нажми «Подписаться» "
            "и вернись сюда.",
            show_alert=True,
        )
        return
    user_id = await db.get_or_create_user(
        callback.from_user.id, callback.from_user.username,
    )
    prefs = await db.get_user_prefs(user_id)
    # Defensive: a stale gate-screen click from a queued callback
    # could otherwise let a user past the channel gate without first
    # accepting the current ToS version. cmd_start enforces this in
    # the normal flow; backstop it here too.
    if not await _reconsent_passes(callback, prefs):
        await callback.answer()
        return
    await callback.answer("✅ Спасибо! Доступ открыт.")
    if prefs.get("onboarded"):
        await _show_main_menu(callback, prefs)
    else:
        await _show_hero(callback)


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
        # Stamping the policy version here is the 152-ФЗ Art. 9 consent
        # capture: from this moment we can prove which version of the
        # public offer + privacy policy the user agreed to.
        await db.set_user_onboarded(
            user_id, True, policy_version=config.consent_policy_version,
        )
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
    ("trial",    "🎁 Пробный",          "Бесплатно",     "1 ссылка-поиск, 6 часов",     "(только один раз)"),
    ("basic",    "💎 Базовый",          "1 290 ₽/мес",   "1 ссылка-поиск, 30 дней",     None),
    ("advanced", "⚡ Продвинутый",       "1 990 ₽/мес",   "3 ссылки-поиска, 30 дней",    None),
    ("pro",      "👑 Профессиональный", "2 990 ₽/мес",   "5 ссылок-поисков, 30 дней",   None),
]

# tariff_id → (max_subs, hours, price_kopeks). `legacy` is the
# grandfathered tier auto-assigned to users who registered before the
# paywall existed. Free state (DB row is NULL or expired) maps to 0
# subs — user has to activate Trial or buy a tier.
_TARIFF_RULES: dict[str, dict] = {
    "trial":    {"max_subs": 1,   "hours": 6,        "kopeks": 0},
    "basic":    {"max_subs": 1,   "hours": 30 * 24,  "kopeks": 129000},
    "advanced": {"max_subs": 3,   "hours": 30 * 24,  "kopeks": 199000},
    "pro":      {"max_subs": 5,   "hours": 30 * 24,  "kopeks": 299000},
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


async def _npd_ceiling_hit() -> bool:
    """Soft block on new sales when the 12-month rolling revenue
    would push the operator over the 2.4M ₽/year НПД cap. Going over
    auto-revokes the самозанятый status with FNS — we don't want to
    accidentally trigger that. Returns True when sales should be
    rejected for compliance reasons."""
    if config.npd_annual_limit_rub <= 0:
        return False
    minor = await db.get_revenue_minor_last_n_days(365)
    return minor >= config.npd_annual_limit_rub * 100  # kopeks vs rub


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

    # Re-consent gate: a stale «buy:trial» button URL or a queued
    # callback from before R14 must not let the user activate trial
    # without accepting the current ToS version. Same logic as
    # cmd_start — admins are bypassed inside _reconsent_passes.
    prefs = await db.get_user_prefs(user_id)
    if not await _reconsent_passes(callback, prefs):
        await callback.answer()
        return

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

    # NPD ceiling — refuse new paid sales once the 12-month rolling
    # revenue would put the operator over the 2.4M ₽ self-employed
    # cap. Crossing it auto-revokes НПД status; admins are allowed
    # past so the operator can still test and process edge cases.
    if not is_admin(callback.from_user.id) and await _npd_ceiling_hit():
        contact = config.support_handle or "поддержку"
        await _present(
            callback,
            f"⚠️ Приём оплат временно приостановлен по техническим "
            f"причинам. Напиши {contact} — поможем оформить тариф вручную.",
            keyboard=back_to_menu_keyboard(),
        )
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


_PAYMENT_COOLDOWN_SECONDS = 60.0
_PAYMENT_BUCKET_MAX = 1000
_LAST_PAYMENT_AT: dict[int, float] = {}


def _payment_cooldown_remaining(tg_id: int) -> int:
    """Return seconds remaining on the per-user payment cooldown, or 0
    if the user is allowed to create a payment right now.

    Why this exists: every click on a "buy" button hits YooKassa's
    /v3/payments endpoint and (under Мой налог) writes a fiscal-receipt
    line item — auto-issued through ОФД. A spamming user can rack up
    dozens of pending fiscal documents on the operator's tax account
    and burn the YooKassa per-shop API quota at the same time. 60s
    between create-payment calls per Telegram user is a generous
    legitimate cadence (a real human filling out an email FSM and
    clicking takes longer) and a hard wall against autoclickers.

    LRU eviction caps memory at ~1000 entries; on a hot bot we
    over-evict on any call that sees the dict over the cap.
    """
    now = time.monotonic()
    last = _LAST_PAYMENT_AT.get(tg_id, 0.0)
    elapsed = now - last
    if elapsed >= _PAYMENT_COOLDOWN_SECONDS:
        _LAST_PAYMENT_AT[tg_id] = now
        if len(_LAST_PAYMENT_AT) > _PAYMENT_BUCKET_MAX:
            cutoff = now - _PAYMENT_COOLDOWN_SECONDS * 4
            for uid in [u for u, t in _LAST_PAYMENT_AT.items() if t < cutoff]:
                del _LAST_PAYMENT_AT[uid]
        return 0
    return int(_PAYMENT_COOLDOWN_SECONDS - elapsed) + 1


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
    # Per-user cooldown — see _payment_cooldown_remaining for rationale.
    # This is the only place that calls yk.create_payment, so gating
    # here covers every entry point (tariffs button, email FSM finish,
    # rebuy after refund). Admins are not exempted: the cooldown is
    # 60s and even the operator should not need to mint payments faster.
    cooldown = _payment_cooldown_remaining(target.from_user.id)
    if cooldown > 0:
        msg = (
            f"⏳ Подожди {cooldown} сек. перед следующей попыткой оплаты — "
            f"защита от случайных дублей."
        )
        if isinstance(target, CallbackQuery):
            await target.answer(msg, show_alert=True)
        else:
            await target.answer(msg)
        return

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

    # Both error paths below render an actionable screen with a retry
    # button instead of a transient toast. Reusing `buy:<tariff_id>`
    # as callback_data sends the user straight back through the same
    # flow — and if their email is already saved, it skips the FSM
    # ask and goes directly to YooKassa create_payment.
    retry_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔁 Попробовать снова", callback_data=f"buy:{tariff_id}",
        )],
        [InlineKeyboardButton(text="⬅️ К тарифам",    callback_data="menu:tariffs")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
    ])

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
        # Clear the cooldown on failure so the user can hit «Попробовать
        # снова» immediately. Without this, the cooldown stamp set
        # earlier in _payment_cooldown_remaining would lock them out
        # for 60 s right after a YooKassa flake — a confusing
        # dead-end on the money path.
        _LAST_PAYMENT_AT.pop(target.from_user.id, None)
        await _present(
            target,
            "❌ Не получилось открыть оплату.\n\n"
            "Скорее всего временная проблема со стороны ЮKassa или сети. "
            "Жми «Попробовать снова» — обычно со второй попытки проходит.",
            keyboard=retry_keyboard,
        )
        if isinstance(target, CallbackQuery):
            await target.answer()
        return

    confirmation_url = (payment.get("confirmation") or {}).get("confirmation_url")
    if not confirmation_url:
        logger.error("[payment] no confirmation_url in YooKassa response: %s", payment)
        # Same cooldown-clear logic as the YooKassaError branch — a
        # malformed YooKassa response shouldn't lock the user out.
        _LAST_PAYMENT_AT.pop(target.from_user.id, None)
        await _present(
            target,
            "❌ ЮKassa не вернула ссылку на оплату.\n\n"
            "Жми «Попробовать снова» — это редкая ошибка, обычно сразу проходит.",
            keyboard=retry_keyboard,
        )
        if isinstance(target, CallbackQuery):
            await target.answer()
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
    if await _warn_if_url_during_fsm(message, "email для чека"):
        return
    # Lowercase the email so YooKassa fiscal receipts match across
    # repeat purchases by the same user typing different cases on
    # different devices (`Foo@bar.co` vs `foo@bar.co` would otherwise
    # produce two distinct customer records and split fiscal history).
    raw = (message.text or "").strip().lower()
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
        "/list — мои поиски (▶️/⏸ ставить на паузу, ✏️ переименовать, "
        "🚫 стоп-слова, ❌ удалить)\n"
        "/profile — язык, валюта, часовой пояс\n"
        "/stop — приостановить все поиски одной командой\n"
        "/cancel — отменить текущий ввод (имя, стоп-слова, email)\n"
        "/help — эта справка\n\n"
        "<b>Данные и приватность:</b>\n"
        "/export_my_data — выгрузка всех твоих данных в JSON\n"
        "/delete_my_account — удалить аккаунт и все подписки"
    )
    if config.support_handle:
        body += f"\n\n💬 <b>Поддержка:</b> {config.support_handle}"

    legal_lines = []
    if config.offer_url:
        legal_lines.append(
            f"📄 <a href=\"{_html.escape(config.offer_url)}\">Публичная оферта</a>"
        )
    if config.privacy_url:
        legal_lines.append(
            f"🔒 <a href=\"{_html.escape(config.privacy_url)}\">"
            "Политика обработки персональных данных</a>"
        )
    if legal_lines:
        body += "\n\n" + "\n".join(legal_lines)

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
    # Show BOTH active and paused (is_active=FALSE but not deleted)
    # so the user can resume any sub they paused. Sort active first
    # to keep the live ones at the top of the screen.
    visible = [s for s in subs if not s.get("deleted")]
    visible.sort(key=lambda s: (not s["is_active"], -(s["id"] or 0)))

    if not visible:
        text = (
            "📋 <b>У тебя нет поисков.</b>\n\n"
            "Пришли ссылку на поиск с любого поддерживаемого маркетплейса — "
            "я начну мониторить."
        )
        await _present(target, text, keyboard=back_to_menu_keyboard())
        return

    lines = ["📋 <b>Мои поиски:</b>\n"]
    sub_buttons = []
    for i, sub in enumerate(visible, 1):
        checked = sub["last_checked_at"]
        checked_str = (
            checked.strftime("%d.%m %H:%M") if checked else "ещё не проверялась"
        )
        errors = (
            f" ⚠️ ошибок: {sub['error_count']}" if sub["error_count"] > 0 else ""
        )
        name = _sub_display_name(sub)
        # Stop-word badge: number of active blacklist entries. JSONB
        # comes back as either str or list; both are handled.
        bl_raw = sub.get("filter_blacklist")
        if isinstance(bl_raw, str):
            try:
                bl_raw = json.loads(bl_raw)
            except Exception:
                bl_raw = []
        bl_count = len(bl_raw) if isinstance(bl_raw, list) else 0
        # Spell out the action — bare "🚫 3" is ambiguous (block what?
        # mute? delete?). Tell the user what's behind the button.
        bl_label = (
            f"🚫 Стоп-слова ({bl_count})" if bl_count else "🚫 Стоп-слова"
        )
        # Names + URLs come from the user / marketplace, escape before
        # inlining into HTML mode. Inline-button text is plain (Telegram
        # doesn't parse HTML there) so the name in callback button is
        # left raw.
        safe_name = _html.escape(name)
        safe_url = _html.escape(sub["url"], quote=True)
        is_paused = not sub.get("is_active", True)
        status_prefix = "⏸ " if is_paused else ""
        lines.append(
            f"<b>{i}.</b> {status_prefix}<a href=\"{safe_url}\">{safe_name}</a>\n"
            f"   Последняя проверка: {checked_str}{errors}"
            + ("\n   <i>На паузе</i>" if is_paused else "")
        )
        # Per-sub controls: rename + stop-words on row 1; pause/resume
        # toggle on row 2 (taking the full width so it's the obvious
        # next thing to tap).
        toggle_label = "▶️ Возобновить" if is_paused else "⏸ Пауза"
        sub_buttons.append([
            InlineKeyboardButton(
                text=f"✏️ {name[:16]}",
                callback_data=f"rename:{sub['id']}",
            ),
            InlineKeyboardButton(
                text=bl_label,
                callback_data=f"bl:{sub['id']}",
            ),
        ])
        sub_buttons.append([
            InlineKeyboardButton(
                text=f"{toggle_label} «{name[:18]}»",
                callback_data=f"sub:toggle:{sub['id']}",
            ),
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        *sub_buttons,
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


# ---------------------------------------------------------------------------
# 152-ФЗ Art. 14 — right of access (export) and right of erasure (delete)
# ---------------------------------------------------------------------------
#
# PRIVACY.md commits us to fulfilling these on user request. The
# previous flow was "DM the operator", which technically satisfies
# the law but breaks the 30-day SLA whenever the operator's away.
# Self-service commands are operationally cleaner and audit-trail
# better.

def _serialize_for_export(value):
    """Recursively convert datetime objects to ISO strings for JSON
    output. asyncpg returns datetime/Decimal which json.dumps can't
    handle natively."""
    from decimal import Decimal
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _serialize_for_export(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize_for_export(v) for v in value]
    return value


@router.message(Command("export_my_data"))
async def cmd_export_my_data(message: Message):
    """Send the caller a JSON dump of all their data.

    152-ФЗ §14 §7 grants users the right to obtain copies of the
    personal data being processed about them. We satisfy this with
    a self-service command instead of a manual support workflow.
    """
    if message.chat.type != "private":
        await message.answer(
            "Эта команда работает только в личных сообщениях боту.",
        )
        return
    user_id = await db.get_or_create_user(
        message.from_user.id, message.from_user.username,
    )
    data = await db.export_user_data(user_id)
    if data is None:
        await message.answer("Нет данных для экспорта.")
        return
    payload = json.dumps(_serialize_for_export(data), ensure_ascii=False, indent=2)
    blob = payload.encode("utf-8")
    # Defensive cap: a user with a freak amount of data (or a future
    # bug bloating the export shape) shouldn't be able to OOM the
    # bot or hit Telegram's 50 MB document limit. 20 MB is well above
    # any realistic export and well under the 50 MB API ceiling.
    if len(blob) > 20 * 1024 * 1024:
        logger.warning(
            "[export] payload too large for tg_id=%s (%d bytes)",
            message.from_user.id, len(blob),
        )
        await message.answer(
            "Слишком большой объём данных для пересылки в чат — "
            "напиши в поддержку, выгрузим вручную и пришлём ссылкой."
        )
        return
    file = BufferedInputFile(blob, filename=f"autosearch_export_{user_id}.json")
    await message.answer_document(
        file,
        caption=(
            "Твои данные в формате JSON. Здесь профиль, активные и "
            "удалённые поиски, история платежей.\n\n"
            "Если хочешь полностью удалить аккаунт — "
            "/delete_my_account."
        ),
    )
    # 152-ФЗ Art. 18.1 audit trail — log the access request so РКН
    # can verify subject-rights handling under inspection. Best-effort:
    # if the audit write fails, the export itself is already done; we
    # don't want to undo the legitimate response.
    try:
        await db.log_subject_request(
            telegram_id=message.from_user.id,
            request_type="export",
            outcome={"bytes": len(blob), "subs": len(data.get("subscriptions") or []),
                     "payments": len(data.get("payments") or [])},
        )
    except Exception:
        logger.exception("[gdpr] log_subject_request(export) failed")


@router.message(Command("delete_my_account"))
async def cmd_delete_my_account(message: Message):
    """Show a confirm prompt; the actual erasure runs only on
    explicit callback to avoid accidental data loss.
    """
    if message.chat.type != "private":
        await message.answer(
            "Эта команда работает только в личных сообщениях боту.",
        )
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🗑 Да, удалить навсегда",
            callback_data="gdpr:confirm_delete",
        )],
        [InlineKeyboardButton(text="↩️ Отмена", callback_data="gdpr:cancel_delete")],
    ])
    await message.answer(
        "⚠️ <b>Удаление аккаунта</b>\n\n"
        "Будет удалено:\n"
        "• Профиль (telegram_id, username, email, настройки)\n"
        "• Все поиски (активные и удалённые)\n"
        "• История уведомлений\n\n"
        "Будет сохранено (для налоговой отчётности — НК РФ требует "
        "хранить 4 года):\n"
        "• История оплат — <b>обезличена</b>, без привязки к тебе\n\n"
        "Действие необратимо. Подтверди или отменись.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "gdpr:confirm_delete")
async def callback_gdpr_confirm_delete(callback: CallbackQuery):
    # Capture the telegram_id BEFORE delete_user_data wipes the user
    # row — we need it for the audit-trail INSERT below.
    erasing_tg_id = callback.from_user.id
    user_id = await db.get_or_create_user(
        callback.from_user.id, callback.from_user.username,
    )
    try:
        stats = await db.delete_user_data(user_id, telegram_id=erasing_tg_id)
    except Exception:
        logger.exception("[gdpr] delete_user_data failed for user_id=%d", user_id)
        await callback.answer(
            "Не удалось выполнить удаление. Напиши в поддержку.",
            show_alert=True,
        )
        return
    logger.info(
        "[gdpr] erasure executed: user_id=%d stats=%s",
        user_id, stats,
    )
    # 152-ФЗ Art. 18.1 audit row — written AFTER the user's data is
    # gone (the row references telegram_id, not user_id, so it
    # survives the cascade).
    try:
        await db.log_subject_request(
            telegram_id=erasing_tg_id,
            request_type="erasure",
            outcome={k: v for k, v in stats.items() if v is not None},
        )
    except Exception:
        logger.exception("[gdpr] log_subject_request(erasure) failed")
    await callback.message.edit_text(
        "✅ Аккаунт удалён.\n\n"
        f"Удалено поисков: {stats['subscriptions_deleted']}\n"
        f"Удалено уведомлений: {stats['sent_items_deleted']}\n"
        f"Обезличено платежей: {stats['payments_anonymized']}\n\n"
        "/start запустит регистрацию заново — но это будет новый "
        "аккаунт без истории.",
    )
    await callback.answer()


@router.callback_query(F.data == "gdpr:cancel_delete")
async def callback_gdpr_cancel_delete(callback: CallbackQuery):
    await callback.message.edit_text("Отменено. Аккаунт не тронут.")
    await callback.answer()


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
    # DM-only: the bot's admin panel renders revenue, customer emails,
    # and payment history. If a real admin types `/admin` in a group
    # the bot also got added to, that data leaks to every member of
    # the group. Refuse with a quiet hint instead of silently leaking.
    if message.chat.type != "private":
        await message.answer(
            "Админ-команды работают только в личных сообщениях боту.",
        )
        return

    raw = (message.text or "").strip().split()
    admin_tg = message.from_user.id

    # Audit every admin invocation — 152-ФЗ Art. 18.1 internal
    # access record. Best-effort: audit-log failures must NEVER
    # block the admin path (that's how operators get locked out
    # of triage). The action+target are logged before serving so
    # РКН can reconstruct who saw what when.
    async def _audit(action: str, target: int | None) -> None:
        try:
            await db.log_admin_access(admin_tg, action, target)
        except Exception:
            logger.exception(
                "[admin] audit-log failed admin=%d action=%s target=%r",
                admin_tg, action, target,
            )

    if len(raw) >= 2 and raw[1] == "health":
        await _audit("admin:health", None)
        await _admin_health(message)
        return
    if len(raw) >= 2 and raw[1] == "users":
        await _audit("admin:users", None)
        await _admin_users_list(message)
        return
    if len(raw) >= 3 and raw[1] == "user":
        try:
            target_tg = int(raw[2])
        except ValueError:
            await message.answer("Использование: /admin user &lt;telegram_id&gt;")
            return
        # Bound the value to signed int64 so a 50-digit typo doesn't
        # crash on asyncpg int8 OverflowError downstream — same family
        # as the R10 advisory-lock overflow that hid /start for days.
        if not (-(1 << 63) <= target_tg < (1 << 63)):
            await message.answer("Telegram ID вне допустимого диапазона.")
            return
        await _audit("admin:user", target_tg)
        await _admin_user_detail(message, target_tg)
        return
    await _audit("admin:dashboard", None)
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

    # 12-month rolling sum vs the НПД ceiling — surface the percentage
    # so the operator notices well before the cap auto-revokes their
    # самозанятый status.
    rev_12m_minor = await db.get_revenue_minor_last_n_days(365)
    rev_12m_rub = rev_12m_minor // 100
    cap_rub = config.npd_annual_limit_rub
    pct = (rev_12m_minor / (cap_rub * 100) * 100) if cap_rub > 0 else 0.0
    if pct >= 100:
        cap_line = (
            f"🚨 <b>НПД лимит ПРЕВЫШЕН:</b> {rev_12m_rub:,} / {cap_rub:,} ₽ "
            f"({pct:.0f}%) — новые продажи блокируются. Срочно переходи на ИП."
        )
    elif pct >= 90:
        cap_line = (
            f"⚠️ <b>НПД близко к лимиту:</b> {rev_12m_rub:,} / {cap_rub:,} ₽ "
            f"({pct:.0f}%). Готовься к переходу на ИП."
        )
    else:
        cap_line = (
            f"📈 НПД лимит: {rev_12m_rub:,} / {cap_rub:,} ₽ ({pct:.0f}%)"
        )

    await message.answer(
        f"📊 <b>Админ-панель</b>\n\n"
        f"<b>💰 Выручка</b>\n"
        f"  За всё время: <b>{rev_total:,} ₽</b>\n"
        f"  За 30 дней:   <b>{rev_30d:,} ₽</b>\n"
        f"  Платящих сейчас: <b>{stats.get('active_paid_users', 0):,}</b>\n"
        f"  Уникальных платежей: <b>{stats.get('paid_users_total', 0):,}</b> "
        f"(за 30д: {stats.get('paid_users_30d', 0):,})\n"
        f"  {cap_line}\n\n"
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
        f"/admin user &lt;tg_id&gt; — детали юзера\n"
        f"/admin health — pulse-check инфраструктуры</i>",
        parse_mode="HTML",
    )


def _fmt_age(ts) -> str:
    """«5 мин назад» / «2 ч назад» / «3 дн назад» — short relative
    age renderer for the health screen. Returns «—» when ts is None."""
    if ts is None:
        return "—"
    now = datetime.now(timezone.utc)
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = now - ts
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "только что"
    if seconds < 60:
        return f"{seconds} сек назад"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    days = hours // 24
    return f"{days} дн назад"


def _health_source_marker(last_checked, expected_interval_sec: int = 180) -> str:
    """Pick a green/yellow/red marker for a per-source row based on
    how long since the last successful fetch. The default 180s is 3×
    the 60s scheduler interval — anything older than that is
    suspicious for a source that has at least one active sub."""
    if last_checked is None:
        return "⚪"
    now = datetime.now(timezone.utc)
    if getattr(last_checked, "tzinfo", None) is None:
        last_checked = last_checked.replace(tzinfo=timezone.utc)
    age = (now - last_checked).total_seconds()
    if age < expected_interval_sec:
        return "✅"
    if age < expected_interval_sec * 3:
        return "🟡"
    return "🔴"


async def _admin_health(message: Message):
    """One-shot pulse-check of the bot's infrastructure for the
    operator. Designed for «something feels off, what's broken?»
    diagnostics — green/yellow/red markers per source, money in the
    last hour and 24 hours, NPD ceiling progress, tombstone count.
    Heavier per-user analytics live in /admin and /admin users."""
    try:
        snap = await db.get_health_snapshot()
    except Exception:
        logger.exception("[admin:health] snapshot failed")
        await message.answer(
            "❌ Не удалось собрать health-snapshot — проверь логи.",
        )
        return

    now_msk = datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=3))
    )
    lines: list[str] = [
        f"🩺 <b>Health</b> ({now_msk.strftime('%H:%M:%S %d.%m')} МСК)",
    ]

    # — Sources
    src_rows = snap.get("per_source") or []
    if src_rows:
        lines.append("\n📡 <b>Парсеры</b> (последний успешный fetch)")
        for row in src_rows:
            src = row["source"]
            active = int(row.get("active") or 0)
            paused = int(row.get("paused") or 0)
            last_checked = row.get("last_checked")
            marker = (
                _health_source_marker(last_checked) if active > 0 else "⚪"
            )
            tail = ""
            if paused:
                tail = f"  <i>({paused} paused)</i>"
            lines.append(
                f"{marker} {source_display_name(src)} — "
                f"{_fmt_age(last_checked)}, активных: {active}{tail}"
            )
    else:
        lines.append("\n📡 <b>Парсеры:</b> подписок ещё нет")

    # — Subs aggregate
    s = snap.get("sub_totals") or {}
    lines.append(
        "\n📋 <b>Подписки</b>\n"
        f"  Активных: {int(s.get('active') or 0)}\n"
        f"  На паузе: {int(s.get('paused') or 0)}\n"
        f"  Удалённых (soft): {int(s.get('deleted') or 0)}\n"
        f"  С ошибками (≥3): {int(s.get('errored') or 0)}"
    )

    # — Users
    u = snap.get("user_counts") or {}
    lines.append(
        "\n👥 <b>Юзеры</b>\n"
        f"  Всего: {int(u.get('total') or 0)} "
        f"(новых за 24ч: {int(u.get('new_24h') or 0)})\n"
        f"  Платных активных: {int(u.get('paid_active') or 0)}\n"
        f"  На пробном: {int(u.get('trial_active') or 0)}\n"
        f"  Удалённых (tombstone): {int(snap.get('tombstones') or 0)}"
    )

    # — Money
    p1 = snap.get("pay_1h") or {}
    p24 = snap.get("pay_24h") or {}
    r24 = snap.get("refund_24h") or {}
    last_pay = snap.get("last_payment_at")
    lines.append(
        "\n💰 <b>Платежи</b>\n"
        f"  За 1ч: {int(p1.get('cnt') or 0)} "
        f"({(int(p1.get('sum_minor') or 0)) // 100} ₽)\n"
        f"  За 24ч: {int(p24.get('cnt') or 0)} "
        f"({(int(p24.get('sum_minor') or 0)) // 100} ₽)\n"
        f"  Возвраты за 24ч: {int(r24.get('cnt') or 0)} "
        f"({(int(r24.get('sum_minor') or 0)) // 100} ₽)\n"
        f"  Последний платёж: {_fmt_age(last_pay)}"
    )

    # — НПД ceiling. Format thousands with thin spaces so the operator
    # can read "2 400 000" at a glance instead of "2400000".
    def _rub(n: int) -> str:
        return f"{n:_}".replace("_", " ")

    npd_minor = int(snap.get("npd_minor") or 0)
    npd_limit_minor = max(1, config.npd_annual_limit_rub * 100)
    npd_pct = round(npd_minor * 100 / npd_limit_minor, 1)
    npd_marker = "✅" if npd_pct < 80 else ("🟡" if npd_pct < 100 else "🔴")
    lines.append(
        "\n🪙 <b>НПД (за 365 дней)</b>\n"
        f"  {npd_marker} {_rub(npd_minor // 100)} ₽ / "
        f"{_rub(config.npd_annual_limit_rub)} ₽ ({npd_pct}%)"
    )

    # — Config gates
    lines.append(
        "\n⚙️ <b>Конфиг</b>\n"
        f"  REQUIRED_CHANNEL: {config.required_channel or '—'}\n"
        f"  Disabled sources: "
        f"{', '.join(config.disabled_sources) if config.disabled_sources else '—'}\n"
        f"  Sentry: {'✅' if config.sentry_dsn else '⚪'}\n"
        f"  Policy ver.: {config.consent_policy_version}"
    )

    await message.answer("\n".join(lines), parse_mode="HTML")


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
    if message.chat.type != "private":
        await message.answer(
            "Админ-команды работают только в личных сообщениях боту.",
        )
        return

    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        await message.answer(
            "Использование: <code>/testbuy &lt;tariff_id&gt;</code>\n\n"
            "<b>Доступные:</b>\n"
            "• <code>/testbuy trial</code> — 0 ₽ (один раз на юзера)\n"
            "• <code>/testbuy basic</code> — 1 290 ₽\n"
            "• <code>/testbuy advanced</code> — 1 990 ₽\n"
            "• <code>/testbuy pro</code> — 2 990 ₽\n\n"
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


@router.callback_query(F.data.startswith("sub:toggle:"))
async def callback_sub_toggle(callback: CallbackQuery):
    """Pause / resume a single subscription. Same ownership-check
    pattern as `del:` — never trust the caller's id from callback_data,
    always resolve the caller and let the DB enforce ownership."""
    try:
        sub_id = int(callback.data.split(":")[2])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID")
        return
    user_id = await db.get_or_create_user(
        callback.from_user.id, callback.from_user.username,
    )
    state = await db.toggle_subscription_active(sub_id, user_id)
    if state is None:
        await callback.answer("Поиск не найден", show_alert=True)
        return
    await callback.answer(
        "▶️ Возобновлено" if state == "resumed" else "⏸ На паузе",
    )
    await _show_subscription_list(callback)


@router.callback_query(F.data.startswith("del:"))
async def callback_delete(callback: CallbackQuery):
    try:
        sub_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID")
        return
    # callback_data is user-controlled — anyone with a Telegram client can
    # send `del:<int>`. Resolve the caller's user_id and let the DB layer
    # enforce ownership; deny silently when no row matches so we don't leak
    # whether sub_id exists for some other user.
    user_id = await db.get_or_create_user(
        callback.from_user.id, callback.from_user.username,
    )
    ok = await db.deactivate_subscription(sub_id, user_id)
    if not ok:
        await callback.answer("Поиск не найден", show_alert=True)
        return
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
    if await _warn_if_url_during_fsm(message, "название поиска"):
        return
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
        await message.answer(
            "Что-то пошло не так — переименование сбросилось. Открой "
            "«Мои поиски» и попробуй заново.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои поиски", callback_data="menu:list")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
            ]),
        )
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
# Stop-words / blacklist (per-subscription) — UX flow
# ---------------------------------------------------------------------------
#
#   /list  →  [✏️ Sub1]  [🚫 N]   ← click 🚫
#                         ↓
#   ┌────────────────────────────────┐
#   │ 🚫 Стоп-слова для «Sub1»       │
#   │                                │
#   │ • женский                      │
#   │ • детский                      │
#   │ • унисекс                      │
#   │                                │
#   │ [ ➕ Добавить ]                │
#   │ [ ❌ женский ] [ ❌ детский ]  │
#   │ [ ❌ унисекс ]                 │
#   │ [ 🧹 Очистить всё ]            │
#   │ [ ⬅️ К поискам ]               │
#   └────────────────────────────────┘
#
# ➕ Добавить → BlacklistStates FSM → user sends "слово1, слово2"
# ❌ <word>   → drops just that one word
# 🧹 Очистить → wipes the list
#
# Filter is substring (not word-boundary) so `женск` catches
# `женский / женское / женская`. Applied in scheduler._process_items
# on raw item title+description (pre-translation), so stop-words
# should be in the marketplace's native language (RU for Avito,
# PL for OLX-PL, etc.).


_BLACKLIST_SCREEN_NOTE = (
    "🚫 <b>Стоп-слова</b> для поиска <b>{name}</b>\n"
    "<i>(только для этого поиска — на другие подписки не влияют)</i>\n\n"
    "Объявления, в заголовке или описании которых встречается "
    "хотя бы одно из этих слов, не будут приходить <b>именно "
    "из этого поиска</b>.\n\n"
    "Сейчас в списке: <b>{count}</b> {plural}.\n"
)


def _ru_plural_words(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "слово"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return "слова"
    return "слов"


async def _show_blacklist_screen(target, sub_id: int):
    user_id = await db.get_or_create_user(
        target.from_user.id, target.from_user.username,
    )
    sub = await db.get_subscription_owned_by(sub_id, user_id)
    if sub is None:
        if isinstance(target, CallbackQuery):
            await target.answer("Поиск не найден", show_alert=True)
        return
    words = await db.get_subscription_blacklist(sub_id, user_id) or []

    name = _sub_display_name(dict(sub))
    text = _BLACKLIST_SCREEN_NOTE.format(
        name=_html.escape(name),
        count=len(words),
        plural=_ru_plural_words(len(words)),
    )
    if words:
        text += (
            "\n<b>Стоп-слова в этом поиске:</b>\n"
            + "\n".join(f"🚫 {_html.escape(w)}" for w in words)
            + "\n\n<i>Нажми кнопку «❌ Удалить стоп-слово ...» чтобы "
              "снять конкретное слово.</i>"
        )
    else:
        text += (
            "\n<i>Список пуст. Нажми «Добавить» и пришли слова через "
            "запятую — например, «женский, детский, фейк».</i>"
        )

    rows: list[list[InlineKeyboardButton]] = [[
        InlineKeyboardButton(
            text="➕ Добавить стоп-слово",
            callback_data=f"bl_add:{sub_id}",
        ),
    ]]
    # One ❌ button per line so the action ("Удалить стоп-слово ‹word›")
    # is unambiguous. Telegram inline buttons clip at ~30 chars, so we
    # truncate the word but keep the verb. Callback key is a content
    # hash of the word, NOT the list index — index-based keys race with
    # rapid double-tap (Telegram retries callbacks on weak connectivity)
    # and end up deleting the wrong word after a list-shift.
    for w in words:
        rows.append([InlineKeyboardButton(
            text=f"❌ Удалить стоп-слово «{w[:14]}»",
            callback_data=f"bl_del:{sub_id}:{_word_hash(w)}",
        )])
    if words:
        rows.append([InlineKeyboardButton(
            text="🧹 Очистить весь стоп-лист",
            callback_data=f"bl_clear:{sub_id}",
        )])
    rows.append([InlineKeyboardButton(
        text="⬅️ К поискам", callback_data="menu:list",
    )])

    await _present(target, text, keyboard=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("bl:"))
async def callback_show_blacklist(callback: CallbackQuery):
    try:
        sub_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID")
        return
    await _show_blacklist_screen(callback, sub_id)
    await callback.answer()


@router.callback_query(F.data.startswith("bl_add:"))
async def callback_blacklist_add(callback: CallbackQuery, state: FSMContext):
    try:
        sub_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID")
        return
    user_id = await db.get_or_create_user(
        callback.from_user.id, callback.from_user.username,
    )
    # Ownership pre-check before opening FSM — saves user from typing
    # words for a sub they don't own.
    sub = await db.get_subscription_owned_by(sub_id, user_id)
    if sub is None:
        await callback.answer("Поиск не найден", show_alert=True)
        return
    await state.set_state(BlacklistStates.waiting_for_words)
    await state.update_data(blacklist_sub_id=sub_id)
    sub_name = _sub_display_name(dict(sub))
    await callback.message.answer(
        f"Добавляем стоп-слова <b>только для поиска «{_html.escape(sub_name)}»</b> "
        f"(на другие подписки не повлияет).\n\n"
        f"Пришли слова через запятую или с новой строки. Например:\n"
        f"<code>женский, детский, фейк</code>\n\n"
        f"Регистр не важен. Поиск идёт по подстроке — «женск» "
        f"поймает все варианты («женский», «женское»…).\n\n"
        f"Ограничения: 2–30 символов на слово, до 50 слов.\n"
        f"<code>/cancel</code> — отмена.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("bl_del:"))
async def callback_blacklist_del(callback: CallbackQuery):
    parts = callback.data.split(":", 2)
    if len(parts) < 3:
        await callback.answer("Неверный ID")
        return
    try:
        sub_id = int(parts[1])
    except ValueError:
        await callback.answer("Неверный ID")
        return
    target_hash = parts[2]
    user_id = await db.get_or_create_user(
        callback.from_user.id, callback.from_user.username,
    )
    words = await db.get_subscription_blacklist(sub_id, user_id)
    if words is None:
        await callback.answer("Поиск не найден", show_alert=True)
        return
    # Content-hash lookup — immune to list shifts under rapid taps.
    # If two taps for different words land within ms, each removes its
    # own word; if two taps for the SAME word land, the second sees
    # "already snipped" and the user gets a clear toast instead of
    # silently deleting a different word.
    new_words = [w for w in words if _word_hash(w) != target_hash]
    if len(new_words) == len(words):
        await callback.answer("Слово уже снято")
    else:
        removed_words = [w for w in words if _word_hash(w) == target_hash]
        await db.set_subscription_blacklist(sub_id, user_id, new_words)
        removed_label = removed_words[0][:30] if removed_words else "слово"
        await callback.answer(f"Снято: {removed_label}")
    await _show_blacklist_screen(callback, sub_id)


@router.callback_query(F.data.startswith("bl_clear:"))
async def callback_blacklist_clear(callback: CallbackQuery):
    try:
        sub_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID")
        return
    user_id = await db.get_or_create_user(
        callback.from_user.id, callback.from_user.username,
    )
    ok = await db.set_subscription_blacklist(sub_id, user_id, [])
    if not ok:
        await callback.answer("Поиск не найден", show_alert=True)
        return
    await callback.answer("Список очищен")
    await _show_blacklist_screen(callback, sub_id)


@router.message(Command("cancel"), StateFilter(BlacklistStates.waiting_for_words))
async def cmd_cancel_blacklist(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Отменено.", reply_markup=back_to_menu_keyboard())


@router.message(StateFilter(BlacklistStates.waiting_for_words), F.text)
async def handle_blacklist_input(message: Message, state: FSMContext):
    if await _warn_if_url_during_fsm(message, "стоп-слова"):
        return
    raw = (message.text or "").strip()
    data = await state.get_data()
    sub_id = data.get("blacklist_sub_id")
    await state.clear()
    if sub_id is None:
        await message.answer(
            "Что-то пошло не так — открой 🚫 у поиска заново.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📋 Мои поиски", callback_data="menu:list"),
            ]]),
        )
        return
    user_id = await db.get_or_create_user(
        message.from_user.id, message.from_user.username,
    )
    existing = await db.get_subscription_blacklist(int(sub_id), user_id)
    if existing is None:
        await message.answer(
            "Поиск не найден или удалён.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    # Normalize the user's text into stop-words and union with existing.
    # The DB-side _normalize_blacklist will dedup + truncate to 50.
    new_words = db._normalize_blacklist(raw)
    if not new_words:
        await message.answer(
            "Не нашёл подходящих слов (нужно 2–30 символов на слово). "
            "Открой 🚫 у поиска заново.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📋 Мои поиски", callback_data="menu:list"),
            ]]),
        )
        return
    merged = list(dict.fromkeys(existing + new_words))  # preserve order, dedup
    ok = await db.set_subscription_blacklist(int(sub_id), user_id, merged)
    if not ok:
        await message.answer(
            "Не удалось сохранить — поиск, видимо, удалён.",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    saved = await db.get_subscription_blacklist(int(sub_id), user_id) or []
    added = len(saved) - len(existing)
    # Re-fetch the sub for its display name so the confirmation makes
    # the per-subscription scope explicit.
    sub_row = await db.get_subscription_owned_by(int(sub_id), user_id)
    sub_name = _sub_display_name(dict(sub_row)) if sub_row else "?"
    await message.answer(
        f"✅ Добавлено: <b>{added}</b>. "
        f"В поиске <b>«{_html.escape(sub_name)}»</b> теперь "
        f"<b>{len(saved)}</b> {_ru_plural_words(len(saved))} "
        f"в стоп-листе.",
        parse_mode="HTML",
    )
    await _show_blacklist_screen(message, int(sub_id))


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

    # Compliance kill-switch: if this source is on the disabled list,
    # reject the new subscription up-front so we don't accumulate
    # entries we're not allowed to scrape.
    if is_source_disabled(source_name):
        pretty = source_display_name(source_name)
        await message.answer(
            f"⚠️ Источник <b>{pretty}</b> временно недоступен. "
            f"Попробуй другой маркетплейс или напиши "
            f"{config.support_handle or 'в поддержку'}.",
            parse_mode="HTML",
            reply_markup=back_to_menu_keyboard(),
        )
        return

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

    # Reject pasting the same URL twice. Without this, a user who clicks
    # «add» twice in a row pays the full add-flow cost both times (initial
    # scrape, fetch_search_items round-trip) and then gets two parallel
    # notification streams of the same listings. find_subscription_by_url
    # ignores deleted=TRUE entries, so a previously-deleted URL can still
    # be re-added.
    existing = await db.find_subscription_by_url(user_id, url)
    if existing is not None:
        if existing["is_active"]:
            hint = (
                "ℹ️ Этот поиск у тебя уже есть и работает. Открой 📋 "
                "«Мои поиски», чтобы посмотреть его."
            )
        else:
            hint = (
                "ℹ️ Этот поиск у тебя уже есть, но сейчас на паузе. Открой "
                "📋 «Мои поиски» и включи его (▶️)."
            )
        await message.answer(
            hint,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои поиски", callback_data="menu:list")],
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
    # Escape with quote=True — the URL goes inside an href="..." attribute,
    # so a `"` in the URL would close the attribute and let an attacker
    # inject a second <a href="https://attacker.com/phishing">…</a> that
    # Telegram renders as a clickable phishing link inside the bot's own
    # confirmation message. _GENERIC_URL_RE accepts any non-whitespace, so
    # the raw value can contain quotes; never trust it as-is here.
    safe_url = _html.escape(url, quote=True)
    await message.answer(
        f"✅ <b>Мониторинг запущен</b> ({pretty})\n\n"
        f"🔗 <a href=\"{safe_url}\">Твоя ссылка</a>\n\n"
        f"Записал {seeded} текущих объявлений как уже виденные. "
        f"Как появится новое — пришлю с фото, ценой и описанием.\n\n"
        f"<i>Хочешь дать поиску своё название? Жми ✏️</i>",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )
