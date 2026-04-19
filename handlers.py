"""Telegram bot handlers — aiogram 3."""
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

from config import config
from database import db
from parser import detect_source, fetch_search_items, supported_sources
from parsers.common import proxy_for_source

logger = logging.getLogger(__name__)
router = Router()

_GENERIC_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _extract_marketplace_url(message_or_text) -> tuple[str, str] | None:
    """Pull a supported-marketplace URL out of the raw message text/caption.

    Returns (source_name, url) on success, None otherwise. The URL is
    accepted only if one of the registered sources matches it (avito,
    kufar, olx, vinted, mercari, ...).

    Reads from message.text / message.caption directly; never from
    message.entities (clipped for long URLs)."""
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
    for ch in ("\u200B", "\u200C", "\u200D", "\u2060", "\u00AD",
               "\uFEFF", "\u00A0", "\u2028", "\u2029"):
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


# Back-compat shim — old code expected just the URL string.
def _extract_avito_url(message_or_text) -> str | None:
    result = _extract_marketplace_url(message_or_text)
    return result[1] if result else None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = await db.get_or_create_user(
        message.from_user.id, message.from_user.username,
    )
    reactivated = await db.reactivate_all(user_id)

    if reactivated > 0:
        await message.answer(
            f"▶️ <b>Возобновлено {reactivated} отслеживаний!</b>\n\n"
            "Мониторинг запущен. Новые объявления придут автоматически.\n\n"
            "/list — активные отслеживания\n"
            "/delete — удалить\n"
            "/stop — пауза",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "<b>Avito Monitor</b> — мгновенные уведомления о новых объявлениях\n\n"
            "<b>Как добавить отслеживание:</b>\n"
            "1. Настрой поиск на Авито (город, категория, цена, фильтры)\n"
            "2. Скопируй ссылку из адресной строки\n"
            "3. Отправь её сюда — длинные ссылки тоже принимаются\n\n"
            f"Лимит: {config.max_subscriptions} отслеживаний одновременно\n\n"
            "/list — мои отслеживания\n"
            "/profile — статистика\n"
            "/stop — пауза",
            parse_mode="HTML",
        )


@router.message(Command("profile"))
async def cmd_profile(message: Message):
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
        f"📊 Всего отслеживаний: <b>{profile['total_subs']}</b>\n"
        f"🟢 Активных сейчас: <b>{profile['active_subs']}</b>\n"
        f"📨 Объявлений найдено: <b>{profile['total_found']}</b>\n"
        f"🕐 Последнее найденное: <b>{last_found_str}</b>",
        parse_mode="HTML",
    )


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
    user_id = await db.get_or_create_user(
        message.from_user.id, message.from_user.username,
    )
    subs = await db.get_user_subscriptions(user_id)
    active = [s for s in subs if s["is_active"]]

    if not active:
        await message.answer(
            "У тебя нет активных отслеживаний. Отправь ссылку с Авито, чтобы начать."
        )
        return

    lines = ["📋 <b>Активные отслеживания:</b>\n"]
    for i, sub in enumerate(active, 1):
        checked = sub["last_checked_at"]
        checked_str = checked.strftime("%d.%m %H:%M") if checked else "ещё не проверялась"
        errors = f" ⚠️ ошибок: {sub['error_count']}" if sub["error_count"] > 0 else ""
        lines.append(
            f"{i}. <a href=\"{sub['url']}\">Ссылка #{sub['id']}</a>\n"
            f"   Последняя проверка: {checked_str}{errors}"
        )

    await message.answer(
        "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True,
    )


@router.message(Command("delete"))
async def cmd_delete(message: Message):
    user_id = await db.get_or_create_user(
        message.from_user.id, message.from_user.username,
    )
    subs = await db.get_user_subscriptions(user_id)
    active = [s for s in subs if s["is_active"]]

    if not active:
        await message.answer("Нет активных отслеживаний для удаления.")
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

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Выбери отслеживание для удаления:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("del:"))
async def callback_delete(callback: CallbackQuery):
    try:
        sub_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID")
        return
    await db.deactivate_subscription(sub_id)
    await callback.message.edit_text(f"✅ Отслеживание #{sub_id} удалено.")
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


@router.message(F.text)
async def handle_url(message: Message):
    extracted = _extract_marketplace_url(message)
    if not extracted:
        sources_pretty = ", ".join(supported_sources())
        await message.answer(
            "Отправь ссылку на поиск с одного из поддерживаемых сайтов:\n"
            f"<i>{sources_pretty}</i>\n\n"
            "Например: <code>https://www.avito.ru/moskva/kvartiry</code>",
            parse_mode="HTML",
        )
        return
    source_name, url = extracted

    user_id = await db.get_or_create_user(
        message.from_user.id, message.from_user.username,
    )

    sub_id = await db.add_subscription(user_id, url, source=source_name)
    if sub_id is None:
        await message.answer(
            f"⚠️ Достигнут лимит — максимум {config.max_subscriptions} отслеживаний.\n"
            "Удали лишние через /delete"
        )
        return

    await message.answer(
        f"⏳ <b>Отслеживание #{sub_id} добавлено</b> ({source_name})\n"
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
        f"🔗 <a href=\"{url}\">Твоя ссылка на Авито</a>\n\n"
        f"Записал {seeded} текущих объявлений как уже виденные. "
        f"Как появится новое — пришлю с фото, ценой и описанием.\n\n"
        f"/list — все отслеживания  ·  /delete — удалить",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
