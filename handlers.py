"""Telegram bot handlers — aiogram 3."""
import base64
import logging
import re
from urllib.parse import urlparse, parse_qs, urlencode

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

logger = logging.getLogger(__name__)
router = Router()

_AVITO_URL_RE = re.compile(r"https?://(?:www\.|m\.)?avito\.ru/\S+", re.IGNORECASE)


def _validate_avito_url(text: str) -> tuple[str | None, str | None]:
    """Extract & clean an Avito URL. Returns (clean_url, error_message)."""
    text = text.strip()
    match = _AVITO_URL_RE.search(text)
    if not match:
        return None, "Это не ссылка на Авито. Отправь ссылку вида https://www.avito.ru/..."

    url = match.group(0).rstrip(".,);]")

    # Telegram sometimes swaps URL-safe base64 `-` with `~`
    url = url.replace("~", "-")

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    # Keep only meaningful params
    keep_keys = ["f", "q", "pmin", "pmax", "s", "user", "bt", "cd"]
    clean: dict[str, str] = {}
    for key in keep_keys:
        if key in qs:
            clean[key] = qs[key][0]

    # Validate that the f= filter is not truncated (Telegram mangling)
    if "f" in clean:
        f_val = clean["f"]
        try:
            padded = f_val + "=" * (-len(f_val) % 4)
            raw = base64.urlsafe_b64decode(padded)
            # f= contains a JSON blob — must terminate with "}"
            if not raw.rstrip(b"\x00").endswith(b"}"):
                return None, (
                    "⚠️ Похоже, URL обрезан Telegram.\n\n"
                    "Отправь ссылку ещё раз, обернув её в обратные кавычки:\n"
                    "<code>`https://www.avito.ru/...`</code>"
                )
        except Exception:
            return None, "⚠️ Неверный формат URL — параметр <code>f=</code> повреждён."

    clean_query = urlencode(clean) if clean else ""
    clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if clean_query:
        clean_url += f"?{clean_query}"
    return clean_url, None


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
            "Как это работает:\n"
            "1. Настрой поиск на Авито (город, категория, цена, фильтры)\n"
            "2. Скопируй ссылку\n"
            "3. Отправь сюда\n\n"
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
    url, error = _validate_avito_url(message.text or "")
    if error:
        await message.answer(error, parse_mode="HTML")
        return
    if not url:
        await message.answer(
            "Отправь ссылку на поиск Авито.\n\n"
            "Например: <code>https://www.avito.ru/moskva/kvartiry</code>",
            parse_mode="HTML",
        )
        return

    user_id = await db.get_or_create_user(
        message.from_user.id, message.from_user.username,
    )

    sub_id = await db.add_subscription(user_id, url)
    if sub_id is None:
        await message.answer(
            f"⚠️ Достигнут лимит — максимум {config.max_subscriptions} отслеживаний.\n"
            "Удали лишние через /delete"
        )
        return

    await message.answer(
        f"✅ <b>Отслеживание #{sub_id} добавлено!</b>\n\n"
        f"🔗 <a href=\"{url}\">Ваша ссылка на Авито</a>\n\n"
        f"Проверяю каждую минуту. Как появится новое объявление — пришлю с фото, ценой и описанием.\n\n"
        f"/list — все отслеживания  ·  /delete — удалить",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
