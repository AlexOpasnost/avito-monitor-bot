import re
import logging

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from database import db
from config import config

logger = logging.getLogger(__name__)
router = Router()

AVITO_URL_PATTERN = re.compile(
    r"https?://(www\.|m\.)?avito\.ru/.+"
)


def _validate_avito_url(text: str) -> str | None:
    """Extract and validate Avito URL from message text."""
    from urllib.parse import urlparse, parse_qs, urlencode
    text = text.strip()
    match = AVITO_URL_PATTERN.search(text)
    if not match:
        return None

    url = match.group(0)

    # Fix Telegram URL mangling: ~ back to - (URL-safe base64 uses - not +)
    url = url.replace("~", "-")

    # Clean URL: keep only useful params (f=, q=, pmin, pmax, s, user, bt)
    # Remove context=, slocation=, etc. (tracking garbage)
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    clean_params = {}
    for key in ["f", "q", "pmin", "pmax", "s", "user", "bt", "cd"]:
        if key in qs:
            clean_params[key] = qs[key][0]

    clean_query = urlencode(clean_params) if clean_params else ""
    clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if clean_query:
        clean_url += f"?{clean_query}"

    return clean_url


AVITO_CATEGORIES = {
    "kvartiry": "Квартиры",
    "komnaty": "Комнаты",
    "doma_dachi_kottedzhi": "Дома, дачи, коттеджи",
    "zemelnye_uchastki": "Земельные участки",
    "garazhi_i_mashinomesta": "Гаражи и машиноместа",
    "kommercheska_nedvizhimost": "Коммерческая недвижимость",
    "nedvizhimost": "Недвижимость",
    "avtomobili": "Автомобили",
    "mototsikly_i_mototehnika": "Мотоциклы и мототехника",
    "gruzoviki_i_spetstehnika": "Грузовики и спецтехника",
    "zapchasti_i_aksessuary": "Запчасти и аксессуары",
    "transport": "Транспорт",
    "vakansii": "Вакансии",
    "rezyume": "Резюме",
    "rabota": "Работа",
    "telefony": "Телефоны",
    "kompyutery": "Компьютеры",
    "noutbuki": "Ноутбуки",
    "planshety_i_elektronnye_knigi": "Планшеты",
    "audio_i_video": "Аудио и видео",
    "igry_pristavki_i_programmy": "Игры, приставки",
    "foto_i_videokamery": "Фото и видеокамеры",
    "bytovaya_tehnika": "Бытовая техника",
    "elektronika": "Электроника",
    "odezhda_obuv_aksessuary": "Одежда, обувь, аксессуары",
    "detskaya_odezhda_i_obuv": "Детская одежда и обувь",
    "tovary_dlya_detey_i_igrushki": "Товары для детей",
    "krasota_i_zdorove": "Красота и здоровье",
    "lichnye_veschi": "Личные вещи",
    "mebel_i_interer": "Мебель и интерьер",
    "bytovaya_elektronika": "Бытовая электроника",
    "sport_i_otdyh": "Спорт и отдых",
    "hobbi_i_otdyh": "Хобби и отдых",
    "muzykalnye_instrumenty": "Музыкальные инструменты",
    "knigi_i_zhurnaly": "Книги и журналы",
    "kollektsionirovanie": "Коллекционирование",
    "zhivotnye": "Животные",
    "sobaki": "Собаки",
    "koshki": "Кошки",
    "dlya_doma_i_dachi": "Для дома и дачи",
    "remont_i_stroitelstvo": "Ремонт и строительство",
    "sad_i_ogorod": "Сад и огород",
    "produkty_pitaniya": "Продукты питания",
    "uslugi": "Услуги",
    "predlozheniya_uslug": "Предложения услуг",
    "gotoviy_biznes_i_oborudovanie": "Готовый бизнес",
    # Subcategories
    "muzhskaya_odezhda": "Мужская одежда",
    "zhenskaya_odezhda": "Женская одежда",
    "verhnyaya_odezhda": "Верхняя одежда",
    "kofty_i_futbolki": "Кофты и футболки",
    "pidzhaki_i_kostyumy": "Пиджаки и костюмы",
    "dzhinsy": "Джинсы",
    "bryuki": "Брюки",
    "rubashki": "Рубашки",
    "shorty": "Шорты",
    "sportivnaya_odezhda": "Спортивная одежда",
    "nizhneye_belye": "Нижнее бельё",
    "platya": "Платья",
    "yubki": "Юбки",
    "bluzy_i_rubashki": "Блузы и рубашки",
    "sumki": "Сумки",
    "obuv": "Обувь",
    "aksessuary": "Аксессуары",
    "chasy_i_ukrasheniya": "Часы и украшения",
    "krossovki": "Кроссовки",
    "botinki": "Ботинки",
    "tufli": "Туфли",
    "sapogi": "Сапоги",
    "sandalii": "Сандалии",
    "rasteniya": "Растения",
    "igrovye_pristavki": "Игровые приставки",
    "nastolnye_igry": "Настольные игры",
    "velosipedy": "Велосипеды",
    "muzykalnye_instrumenty": "Музыкальные инструменты",
    "posuда": "Посуда",
    "produkty_pitaniya": "Продукты питания",
    "avtomobili": "Автомобили",
    "televizory": "Телевизоры",
    "stiralnye_mashiny": "Стиральные машины",
    "holodilniki": "Холодильники",
    "smartfony": "Смартфоны",
    "planshety": "Планшеты",
}

AVITO_CITIES = {
    "moskva": "Москва",
    "sankt-peterburg": "Санкт-Петербург",
    "novosibirsk": "Новосибирск",
    "ekaterinburg": "Екатеринбург",
    "kazan": "Казань",
    "nizhniy_novgorod": "Нижний Новгород",
    "chelyabinsk": "Челябинск",
    "samara": "Самара",
    "omsk": "Омск",
    "rostov-na-donu": "Ростов-на-Дону",
    "ufa": "Уфа",
    "krasnoyarsk": "Красноярск",
    "voronezh": "Воронеж",
    "perm": "Пермь",
    "volgograd": "Волгоград",
    "krasnodar": "Краснодар",
    "tyumen": "Тюмень",
    "saratov": "Саратов",
    "tolyatti": "Тольятти",
    "izhevsk": "Ижевск",
    "barnaul": "Барнаул",
    "vladivostok": "Владивосток",
    "irkutsk": "Иркутск",
    "habarovsk": "Хабаровск",
    "yaroslavl": "Ярославль",
    "tomsk": "Томск",
    "orenburg": "Оренбург",
    "novokuznetsk": "Новокузнецк",
    "ryazan": "Рязань",
    "naberezhnye_chelny": "Набережные Челны",
    "kirov": "Киров",
    "sevastopol": "Севастополь",
    "rossiya": "Россия",
}


def _parse_avito_url_info(url: str) -> dict:
    """Extract city, category and query from Avito URL for display."""
    from urllib.parse import urlparse, parse_qs, unquote
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]

    info = {"city": None, "category": None, "query": None}

    if len(path_parts) >= 1:
        slug = path_parts[0].lower()
        if slug == "all":
            info["city"] = "Вся Россия"
        else:
            info["city"] = AVITO_CITIES.get(slug, slug.replace("-", " ").replace("_", " ").title())

    if len(path_parts) >= 2:
        cats = []
        for part in path_parts[1:]:
            # Skip encoded slugs (ASgB...), item URLs (name_12345), prodam/kupit
            if re.match(r'^[A-Z][A-Za-z0-9+/=]+$', part):
                continue
            slug = part.lower()
            if re.match(r'^.+_\d{5,}$', slug):
                continue
            if slug in ("prodam", "kupit", "sdam", "snimu"):
                continue

            # Normalize: try both underscore and hyphen variants
            slug_underscore = slug.replace("-", "_")
            slug_hyphen = slug.replace("_", "-")

            name = (
                AVITO_CATEGORIES.get(slug)
                or AVITO_CATEGORIES.get(slug_underscore)
                or AVITO_CATEGORIES.get(slug_hyphen)
            )
            if name:
                cats.append(name)
            # Don't add raw transliterated slugs — they look ugly
        info["category"] = " → ".join(cats) if cats else None

    # Check for query parameter
    qs = parse_qs(parsed.query)
    if "q" in qs:
        info["query"] = unquote(qs["q"][0])

    return info


@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = await db.get_or_create_user(
        message.from_user.id,
        message.from_user.username,
    )

    # Reactivate paused subscriptions
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
            "Что ты получишь:\n"
            "• Фото, цена, описание, город\n"
            "• Рейтинг и просмотры продавца\n"
            "• Точная дата публикации\n"
            "• Только свежие объявления (до 2 дней)\n"
            f"• До {config.max_subscriptions} отслеживаний одновременно\n\n"
            "/list — мои отслеживания\n"
            "/profile — статистика\n"
            "/stop — пауза",
            parse_mode="HTML",
        )


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    user_id = await db.get_or_create_user(
        message.from_user.id,
        message.from_user.username,
    )
    profile = await db.get_user_profile(user_id)
    user = profile["user"]

    # Registration date
    reg_date = user["created_at"].strftime("%d.%m.%Y") if user["created_at"] else "—"

    # Last found item
    last_found_str = "—"
    if profile["last_found"]:
        last_found_str = profile["last_found"]["sent_at"].strftime("%d.%m.%Y %H:%M")

    name = message.from_user.full_name or user["username"] or "Пользователь"

    await message.answer(
        f"👤 <b>Профиль: {name}</b>\n\n"
        f"📅 Дата регистрации: <b>{reg_date}</b>\n"
        f"📊 Всего отслеживаний создано: <b>{profile['total_subs']}</b>\n"
        f"🟢 Активных сейчас: <b>{profile['active_subs']}</b>\n"
        f"📨 Объявлений найдено: <b>{profile['total_found']}</b>\n"
        f"🕐 Последнее найденное: <b>{last_found_str}</b>\n",
        parse_mode="HTML",
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if config.admin_id == 0 or message.from_user.id != config.admin_id:
        return

    stats = await db.get_admin_stats()

    last_checked_str = "—"
    if stats["last_checked"]:
        from datetime import timezone, timedelta
        msk = timezone(timedelta(hours=3))
        last_checked_str = stats["last_checked"].astimezone(msk).strftime("%H:%M %d.%m.%Y")

    await message.answer(
        f"📊 <b>Админ-панель</b>\n\n"
        f"👥 Юзеров: <b>{stats['total_users']:,}</b>\n"
        f"📋 Активных подписок: <b>{stats['active_subs']:,}</b>\n"
        f"🔗 Уникальных ссылок: <b>{stats['unique_urls']:,}</b>\n"
        f"📨 Объявлений отправлено: <b>{stats['total_sent']:,}</b>\n"
        f"⏱ Последняя проверка: <b>{last_checked_str}</b>\n\n"
        f"📈 <b>За последние 24ч:</b>\n"
        f"  Новых юзеров: <b>{stats['new_users_24h']:,}</b>\n"
        f"  Отправлено: <b>{stats['sent_24h']:,}</b>",
        parse_mode="HTML",
    )


@router.message(Command("list"))
async def cmd_list(message: Message):
    user_id = await db.get_or_create_user(
        message.from_user.id,
        message.from_user.username,
    )
    subs = await db.get_user_subscriptions(user_id)
    active = [s for s in subs if s["is_active"]]

    if not active:
        await message.answer("У тебя нет активных отслеживаний. Отправь ссылку с Авито чтобы начать.")
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

    await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("delete"))
async def cmd_delete(message: Message):
    user_id = await db.get_or_create_user(
        message.from_user.id,
        message.from_user.username,
    )
    subs = await db.get_user_subscriptions(user_id)
    active = [s for s in subs if s["is_active"]]

    if not active:
        await message.answer("Нет активных отслеживаний для удаления.")
        return

    buttons = []
    for sub in active:
        short_url = sub["url"][:50] + "..." if len(sub["url"]) > 50 else sub["url"]
        buttons.append([
            InlineKeyboardButton(
                text=f"❌ #{sub['id']} — {short_url}",
                callback_data=f"del:{sub['id']}",
            )
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Выбери отслеживание для удаления:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("del:"))
async def callback_delete(callback: CallbackQuery):
    sub_id = int(callback.data.split(":")[1])
    await db.deactivate_subscription(sub_id)
    await callback.message.edit_text(f"✅ Отслеживание #{sub_id} удалено.")
    await callback.answer()


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    user_id = await db.get_or_create_user(
        message.from_user.id,
        message.from_user.username,
    )
    await db.deactivate_all(user_id)
    await message.answer(
        "⏸ <b>Мониторинг приостановлен</b>\n\n"
        "Все ссылки сохранены. Нажми /start чтобы возобновить.",
        parse_mode="HTML",
    )


@router.message(F.text)
async def handle_url(message: Message):
    url = _validate_avito_url(message.text)
    if not url:
        await message.answer(
            "Отправь ссылку на поиск Авито.\n\n"
            "Например:\n"
            "<code>https://www.avito.ru/moskva/kvartiry</code>",
            parse_mode="HTML",
        )
        return

    user_id = await db.get_or_create_user(
        message.from_user.id,
        message.from_user.username,
    )

    # Warn if URL has no filters
    from urllib.parse import urlparse, parse_qs
    url_qs = parse_qs(urlparse(url).query)
    if "context" in url_qs and "f" not in url_qs:
        await message.answer(
            "⚠️ Эта ссылка не содержит фильтров.\n\n"
            "Открой Авито, выбери фильтры (бренд, состояние, цену), "
            "затем скопируй ссылку из адресной строки.\n"
            "Правильная ссылка содержит <code>?f=</code> в URL.",
            parse_mode="HTML",
        )
        return

    sub_id = await db.add_subscription(user_id, url)
    if sub_id is None:
        await message.answer(
            f"⚠️ Достигнут лимит — максимум {config.max_subscriptions} отслеживаний.\n"
            "Удали лишние через /delete"
        )
        return

    info = _parse_avito_url_info(url)
    details = []
    if info["city"]:
        details.append(f"📍 <b>Город:</b> {info['city']}")
    if info["category"]:
        details.append(f"📂 <b>Категория:</b> {info['category']}")
    if info["query"]:
        details.append(f"🔍 <b>Запрос:</b> {info['query']}")
    details.append(f"🔗 <a href=\"{url}\">Ваша ссылка на Авито</a>")

    details_text = "\n".join(details)

    await message.answer(
        f"✅ <b>Отслеживание #{sub_id} добавлено!</b>\n\n"
        f"{details_text}\n\n"
        f"Проверяю каждую минуту. Как появится новое объявление — сразу пришлю с фото, ценой и описанием.\n\n"
        f"/list — все отслеживания  ·  /delete — удалить",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
