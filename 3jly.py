#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
"""

import asyncio
import logging
import os
import random
import re
import struct
import sys
import time
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

import aiosqlite
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
    error as tg_error,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# ==============================================================================
# НАСТРОЙКИ И КОНСТАНТЫ
# ==============================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Автоматическая загрузка настроек из файла .env, если он существует
_env_path = os.path.join(BASE_DIR, ".env")
if os.path.isfile(_env_path):
    try:
        with open(_env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
    except Exception:
        pass

# Токен только из окружения или из файла .env рядом со скриптом.
# В коде токен не хранится: тот, что был прописан раньше, нужно считать засвеченным.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Идентификаторы администраторов (автоматический полный доступ только им)
ADMIN_IDS: Set[int] = {8822479121, 8818045521}

# Опциональный прокси (например: http://127.0.0.1:10809 или socks5://127.0.0.1:10808)
PROXY_URL = os.getenv("PROXY_URL", "").strip()

# Опциональный адрес зеркала / reverse proxy (например: https://api.telegram.org/bot или https://myproxy.workers.dev/bot)
BASE_URL = os.getenv("TELEGRAM_API_URL", os.getenv("BASE_URL", "")).strip()

DB_PATH = os.path.join(BASE_DIR, "shop.db")

# Имена файлов фото по умолчанию
FILE_BANNER_MAIN = "banner.jpg"
FILE_BANNER_ASSORT = "banneras.jpg"
FILE_PHOTO_LIR = "lir.jpg"
FILE_PHOTO_WATER = "photo_2026-11-20_14-40-41.jpg"

# Текст приветствия (можно изменить в коде или в админке)
DEFAULT_GREETING = (
    "👋 <b>Добро пожаловать в Pharmojelly Shop!</b>\n\n"
    "Здесь вы найдете качественную продукцию по лучшим ценам.\n"
    "Используйте удобное меню под фотографией для навигации:"
)

# Реквизиты по умолчанию
DEFAULT_TRON_ADDRESS = "TNuD2vFXuWcU6cSpyW8yvQW5sbYuiZDWig"

# Текст "О нас" по умолчанию
DEFAULT_ABOUT_US = (
    "🏢 <b>О нашем магазине Pharmojelly</b>\n\n"
    "Мы работаем на рынке уже долгое время и гарантируем высочайшее качество обслуживания!\n"
    "✔️ Мгновенная обработка заказов\n"
    "✔️ Честность и надежность\n"
    "✔️ Круглосуточная поддержка"
)

# Логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("PharmojellyBot")

# Защита от флуда: время последнего действия пользователя {tg_id: timestamp}
user_last_action: Dict[int, float] = {}

# Верный ответ капчи, если user_data бота сбросился: {tg_id: сумма}
pending_captcha: Dict[int, int] = {}

# Реестр запущенных приложений ботов {token: Application}
running_applications: Dict[str, Application] = {}

# Глобальный объект основного приложения бота (для рассылок и уведомлений админам)
main_app_instance: Optional[Application] = None


# ==============================================================================
# РАБОТА С ФАЙЛАМИ И ФОТО
# ==============================================================================

def get_abs_path(filename: str) -> str:
    """Возвращает абсолютный путь к файлу."""
    if not filename:
        return ""
    if os.path.isabs(filename):
        return filename
    return os.path.join(BASE_DIR, filename)


def _image_ok(path: str) -> bool:
    """Настоящий файл картинки, а не пустышка в несколько сотен байт."""
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 500
    except OSError:
        return False


def _write_placeholder(path: str, width: int = 640, height: int = 360) -> str:
    """
    Пишет нормальный PNG (не 1x1).
    Telegram отклоняет крошечный JPEG ошибкой PHOTO_INVALID_DIMENSIONS,
    и после капчи меню просто не появляется.
    """
    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    rgb = (18, 52, 86)
    row = b"\x00" + bytes(rgb) * width
    raw = row * height
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)
    return path


def resolve_photo_path(photo_name: Optional[str]) -> str:
    """
    Разрешает путь к фото. Если файл не найден, возвращает fallback-изображение (banner.jpg).
    Бот гарантированно всегда найдет валидную фотографию.
    """
    candidates = []
    if photo_name:
        candidates.append(get_abs_path(photo_name))
        candidates.append(get_abs_path(f"{photo_name}.jpg"))

    candidates.extend([
        get_abs_path(FILE_BANNER_MAIN),
        get_abs_path("banner.png"),
        get_abs_path(FILE_BANNER_ASSORT),
        get_abs_path(FILE_PHOTO_LIR),
        get_abs_path(FILE_PHOTO_WATER),
        get_abs_path("photo_2026-11-20_14-36-41.jpg"),
    ])

    for p in candidates:
        if _image_ok(p):
            return p

    placeholder = get_abs_path("banner.png")
    try:
        return _write_placeholder(placeholder)
    except Exception:
        logger.exception("Could not write placeholder image")
        return placeholder


# ==============================================================================
# БАЗА ДАННЫХ
# ==============================================================================

async def init_db():
    """Инициализация и миграция базы данных SQLite."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=5000;")
        # Таблица пользователей
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                account_id TEXT NOT NULL UNIQUE,
                balance REAL NOT NULL DEFAULT 0.0,
                captcha_passed INTEGER DEFAULT 0,
                bonus_claimed INTEGER DEFAULT 0,
                source_bot_token TEXT DEFAULT '',
                created_at TEXT DEFAULT ''
            )
            """
        )

        # Таблица товаров
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                price REAL NOT NULL DEFAULT 0.0,
                photo_path TEXT
            )
            """
        )

        # Таблица отзывов (максимум 5 под каждым товаром)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                user_tg_id INTEGER NOT NULL,
                user_account_id TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        # Таблица заказов
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_tg_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                price REAL NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL,
                taken_by_admin INTEGER DEFAULT 0,
                admin_note TEXT DEFAULT '',
                source_bot_token TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )

        # Переписка по заказу
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS order_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                sender_tg_id INTEGER NOT NULL,
                sender_role TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        # Таблица пополнений (депозитов)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_tg_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                check_id TEXT NOT NULL UNIQUE,
                photo_file_id TEXT DEFAULT '',
                status TEXT NOT NULL,
                source_bot_token TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )

        # Таблица тикетов техподдержки
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_tg_id INTEGER NOT NULL,
                subject TEXT DEFAULT 'Вопрос',
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                closed_at TEXT
            )
            """
        )

        # Сообщения в тикетах
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                sender_tg_id INTEGER NOT NULL,
                sender_role TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        # Таблица подключенных реферальных ботов
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS clone_bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE NOT NULL,
                owner_tg_id INTEGER NOT NULL,
                bot_username TEXT,
                created_at TEXT NOT NULL,
                is_active INTEGER DEFAULT 1
            )
            """
        )

        # Таблица настроек ключ-значение
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

        # Миграция колонок на случай старой структуры
        async def add_column_if_missing(table, col_def):
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
            except Exception:
                pass

        await add_column_if_missing("users", "captcha_passed INTEGER DEFAULT 0")
        await add_column_if_missing("users", "bonus_claimed INTEGER DEFAULT 0")
        await add_column_if_missing("users", "source_bot_token TEXT DEFAULT ''")
        await add_column_if_missing("users", "created_at TEXT DEFAULT ''")

        await add_column_if_missing("products", "price REAL DEFAULT 0.0")

        await add_column_if_missing("reviews", "user_account_id TEXT DEFAULT ''")

        await add_column_if_missing("orders", "price REAL DEFAULT 0.0")
        await add_column_if_missing("orders", "taken_by_admin INTEGER DEFAULT 0")
        await add_column_if_missing("orders", "source_bot_token TEXT DEFAULT ''")

        await add_column_if_missing("deposits", "photo_file_id TEXT DEFAULT ''")
        await add_column_if_missing("deposits", "source_bot_token TEXT DEFAULT ''")

        await add_column_if_missing("tickets", "message TEXT DEFAULT ''")
        await add_column_if_missing("tickets", "subject TEXT DEFAULT 'Вопрос'")
        await add_column_if_missing("tickets", "closed_at TEXT")

        # Начальные конфигурации
        configs_to_seed = {
            "tron_address": DEFAULT_TRON_ADDRESS,
            "about_us": DEFAULT_ABOUT_US,
            "greeting_text": DEFAULT_GREETING,
            "banner_main": FILE_BANNER_MAIN,
            "banner_assort": FILE_BANNER_ASSORT,
            "photo_lir": FILE_PHOTO_LIR,
        }
        for k, v in configs_to_seed.items():
            await db.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))

        # Начальные товары (Конфеты с lir.jpg и Вода)
        cur = await db.execute("SELECT COUNT(*) FROM products")
        cnt = (await cur.fetchone())[0]
        if cnt == 0:
            await db.execute(
                "INSERT INTO products (name, description, price, photo_path) VALUES (?, ?, ?, ?)",
                ("Конфеты", "Вкусные жевательные конфеты премиум качества.", 150.0, FILE_PHOTO_LIR),
            )
            await db.execute(
                "INSERT INTO products (name, description, price, photo_path) VALUES (?, ?, ?, ?)",
                ("Вода", "Минеральная негазированная вода 0.5 л.", 80.0, FILE_PHOTO_WATER),
            )
        else:
            # Обновим цену и фото для дефолтных, если price был 0
            await db.execute("UPDATE products SET price = 150.0, photo_path = ? WHERE name = 'Конфеты' AND price = 0", (FILE_PHOTO_LIR,))
            await db.execute("UPDATE products SET price = 80.0, photo_path = ? WHERE name = 'Вода' AND price = 0", (FILE_PHOTO_WATER,))

        await db.commit()


async def get_config(key: str, default: str = "") -> str:
    """Получение значения настройки из БД."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default


async def set_config(key: str, value: str):
    """Сохранение настройки в БД."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        await db.commit()


def gen_account_id() -> str:
    """Генерация уникального ID аккаунта вида #F89JHT."""
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "#" + "".join(random.choices(chars, k=6))


def gen_check_id() -> str:
    """Генерация уникального чека пополнения."""
    chars = "0123456789ABCDEF"
    return "#TRX" + "".join(random.choices(chars, k=8))


async def get_or_create_user(tg_id: int, source_bot_token: str = "") -> dict:
    """Получить или создать пользователя в БД."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT tg_id, account_id, balance, captcha_passed, bonus_claimed, source_bot_token FROM users WHERE tg_id = ?",
            (tg_id,),
        )
        row = await cur.fetchone()
        if not row:
            # Генерация уникального account_id
            while True:
                aid = gen_account_id()
                c = await db.execute("SELECT tg_id FROM users WHERE account_id = ?", (aid,))
                if not await c.fetchone():
                    break
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "INSERT INTO users (tg_id, account_id, balance, captcha_passed, bonus_claimed, source_bot_token, created_at) "
                "VALUES (?, ?, 0.0, 0, 0, ?, ?)",
                (tg_id, aid, source_bot_token, now),
            )
            await db.commit()
            return {
                "tg_id": tg_id,
                "account_id": aid,
                "balance": 0.0,
                "captcha_passed": 0,
                "bonus_claimed": 0,
                "source_bot_token": source_bot_token,
            }
        return {
            "tg_id": row[0],
            "account_id": row[1],
            "balance": float(row[2]),
            "captcha_passed": int(row[3] or 0),
            "bonus_claimed": int(row[4] or 0),
            "source_bot_token": row[5] or "",
        }


# ==============================================================================
# ЗАЩИТА ОТ БАНА И АНТИФЛУД
# ==============================================================================

def check_anti_flood(user_id: int, min_interval: float = 0.35) -> bool:
    """
    Проверяет, не кликает ли пользователь слишком часто.
    Возвращает True, если действие разрешено, False если заблокировано по антифлуду.
    """
    now = time.time()
    last = user_last_action.get(user_id, 0.0)
    if now - last < min_interval:
        return False
    user_last_action[user_id] = now
    return True


# ==============================================================================
# ДВИЖОК РЕНДЕРИНГА ПОСТОВ (С ФОТО И РЕДАКТИРОВАНИЕМ В ОДНОМ ПОСТЕ)
# ==============================================================================

async def render_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    photo_name_or_path: str,
    caption: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    keep_state: bool = False,
    force_new: bool = False,
):
    """
    Основной метод отображения интерфейса.
    Гарантирует:
    1. Каждое сообщение сопровождается фотографией (по умолчанию banner.jpg).
    2. При переходах по кнопкам бот НЕ шлет новые сообщения, а меняет предыдущий пост.
    3. Если пользователь прислал текст/фото (ввод суммы, скриншот и т.д.), бот удаляет
       сообщение пользователя и редактирует предыдущий пост бота.
    4. Защиту от сбоев Telegram API и 'Message is not modified'.
    5. После текстовой капчи меню уходит новым сообщением, без правки удалённого текста как фото.
    """
    resolved_path = resolve_photo_path(photo_name_or_path)
    user_id = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id if update.effective_chat else user_id

    if not keep_state:
        context.user_data.pop("state", None)

    if force_new:
        context.user_data["menu_msg_id"] = None
        context.user_data["current_photo"] = None

    edited = False

    # 1. Если клик по кнопке (CallbackQuery) и пост уже с фото
    if update.callback_query and not force_new:
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass

        current_photo = context.user_data.get("current_photo")
        has_photo = bool(query.message and query.message.photo)

        if query.message and has_photo:
            # Если фото то же самое — просто меняем подпись (это мгновенно и без задержки)
            if current_photo == resolved_path:
                try:
                    await query.edit_message_caption(
                        caption=caption,
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                    )
                    edited = True
                except tg_error.BadRequest as e:
                    if "Message is not modified" in str(e):
                        edited = True
                except Exception:
                    edited = False

            # Если фото изменилось или edit_caption не прошел — пробуем редактировать медиа
            if not edited and _image_ok(resolved_path):
                try:
                    with open(resolved_path, "rb") as f:
                        await query.edit_message_media(
                            media=InputMediaPhoto(media=f, caption=caption, parse_mode="HTML"),
                            reply_markup=reply_markup,
                        )
                    context.user_data["current_photo"] = resolved_path
                    context.user_data["menu_is_photo"] = True
                    edited = True
                except tg_error.BadRequest as e:
                    if "Message is not modified" in str(e):
                        edited = True
                except Exception as e:
                    logger.debug(f"edit_message_media error: {e}")
                    edited = False

        if edited and query.message:
            context.user_data["menu_msg_id"] = query.message.message_id
            context.user_data["menu_is_photo"] = True
            return

        # Текстовая капча или уже удалённое сообщение — не редактируем его как фото
        try:
            if query.message and not has_photo:
                await query.message.delete()
        except Exception:
            pass

    # 2. Если пользователь прислал текстовое сообщение или файл
    elif update.message and not force_new:
        try:
            await update.message.delete()
        except Exception:
            pass

    # 3. Попытка отредактировать сохраненный menu_msg_id
    menu_msg_id = context.user_data.get("menu_msg_id")
    current_photo = context.user_data.get("current_photo")
    menu_is_photo = context.user_data.get("menu_is_photo", True)

    if menu_msg_id and not force_new:
        if not menu_is_photo:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=menu_msg_id,
                    text=caption,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
                edited = True
            except tg_error.BadRequest as e:
                if "Message is not modified" in str(e):
                    edited = True
            except Exception:
                edited = False
        else:
            if current_photo == resolved_path:
                try:
                    await context.bot.edit_message_caption(
                        chat_id=chat_id,
                        message_id=menu_msg_id,
                        caption=caption,
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                    )
                    edited = True
                except tg_error.BadRequest as e:
                    if "Message is not modified" in str(e):
                        edited = True
                except Exception:
                    edited = False

            if not edited and _image_ok(resolved_path):
                try:
                    with open(resolved_path, "rb") as f:
                        await context.bot.edit_message_media(
                            chat_id=chat_id,
                            message_id=menu_msg_id,
                            media=InputMediaPhoto(media=f, caption=caption, parse_mode="HTML"),
                            reply_markup=reply_markup,
                        )
                    context.user_data["current_photo"] = resolved_path
                    context.user_data["menu_is_photo"] = True
                    edited = True
                except tg_error.BadRequest as e:
                    if "Message is not modified" in str(e):
                        edited = True
                except Exception:
                    edited = False

    # 4. Если отредактировать не удалось, шлем новый пост с фото
    if not edited:
        sent = False
        if _image_ok(resolved_path):
            try:
                with open(resolved_path, "rb") as f:
                    msg = await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=f,
                        caption=caption,
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                    )
                context.user_data["menu_msg_id"] = msg.message_id
                context.user_data["current_photo"] = resolved_path
                context.user_data["menu_is_photo"] = True
                sent = True
            except Exception as e:
                logger.error(f"Failed to send post with photo: {e}")

        if not sent:
            try:
                msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
                context.user_data["menu_msg_id"] = msg.message_id
                context.user_data["current_photo"] = None
                context.user_data["menu_is_photo"] = False
            except Exception as e:
                logger.error(f"Failed to send text menu: {e}")


# ==============================================================================
# КЛАВИАТУРЫ И МЕНЮ
# ==============================================================================

def get_main_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    """Главное меню кнопок."""
    buttons = [
        [InlineKeyboardButton("🛍️ Ассортимент", callback_data="nav_assortment")],
        [
            InlineKeyboardButton("👤 Профиль", callback_data="nav_profile"),
            InlineKeyboardButton("ℹ️ О нас", callback_data="nav_about"),
        ],
        [InlineKeyboardButton("💬 Тех.Поддержка", callback_data="nav_support")],
        [InlineKeyboardButton("🤖 Создать своего бота (+2$)", callback_data="nav_create_bot")],
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton("⚙️ Панель администратора", callback_data="admin_home")])
    return InlineKeyboardMarkup(buttons)


def get_back_button(target: str = "nav_menu", text: str = "🔙 В главное меню") -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=target)


# ==============================================================================
# ОБРАБОТЧИКИ: СТАРТ И КАПЧА (БЕЗ ФОТО, ОТДЕЛЬНЫМ ТЕКСТОВЫМ СООБЩЕНИЕМ)
# ==============================================================================

def _captcha_keyboard(options: List[int]) -> InlineKeyboardMarkup:
    """Две строки по две кнопки. Одна длинная строка на телефоне часто не нажимается."""
    buttons = [
        InlineKeyboardButton(str(opt), callback_data=f"captcha_{opt}")
        for opt in options
    ]
    return InlineKeyboardMarkup([buttons[:2], buttons[2:4]])


async def _answer_query(query, text: str = "") -> None:
    """Гасит спиннер кнопки ровно один раз. Повторный answer не роняет хендлер."""
    try:
        if text:
            await query.answer(text, show_alert=False)
        else:
            await query.answer()
    except Exception:
        pass


async def send_captcha_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    is_wrong: bool = False,
) -> bool:
    """
    Отправляет или обновляет капчу одним текстовым сообщением (без фото).
    Возвращает True, если сообщение удалось показать.
    """
    user = update.effective_user
    if not user:
        return False
    chat_id = update.effective_chat.id if update.effective_chat else user.id

    a = random.randint(1, 9)
    b = random.randint(1, 9)
    correct = a + b
    options = {correct}
    guard = 0
    while len(options) < 4 and guard < 40:
        guard += 1
        low = max(0, correct - 5)
        high = correct + 5
        fake = random.randint(low, high)
        if fake != correct:
            options.add(fake)
    while len(options) < 4:
        extra = (max(options) if options else correct) + 1
        options.add(extra)
    opts_list = list(options)
    random.shuffle(opts_list)

    context.user_data["captcha_correct"] = int(correct)
    pending_captcha[user.id] = int(correct)

    if is_wrong:
        text = (
            "❌ <b>Неверный ответ!</b>\n\n"
            "Решите новый пример:\n\n"
            f"👉 <b>Сколько будет {a} + {b}?</b>"
        )
    else:
        text = (
            "🤖 <b>Добро пожаловать в Pharmojelly Shop!</b>\n\n"
            "Чтобы открыть магазин, решите пример:\n\n"
            f"👉 <b>Сколько будет {a} + {b}?</b>"
        )

    kb = _captcha_keyboard(opts_list)

    query = update.callback_query
    # Редактируем только текстовое сообщение капчи. Фото-пост так не меняется.
    if query and query.message and not query.message.photo:
        try:
            await query.edit_message_text(text=text, reply_markup=kb, parse_mode="HTML")
            context.user_data["captcha_msg_id"] = query.message.message_id
            return True
        except tg_error.BadRequest as e:
            if "Message is not modified" in str(e):
                context.user_data["captcha_msg_id"] = query.message.message_id
                return True
            logger.info(f"edit captcha failed: {e}")
        except Exception as e:
            logger.info(f"edit captcha failed: {e}")
        try:
            await query.message.delete()
        except Exception:
            pass

    prev_id = context.user_data.get("captcha_msg_id")
    if prev_id and (not query or not query.message or prev_id != query.message.message_id):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=prev_id)
        except Exception:
            pass

    try:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=kb,
            parse_mode="HTML",
        )
        context.user_data["captcha_msg_id"] = msg.message_id
        return True
    except Exception as e:
        logger.error(f"Failed to send captcha text message: {e}")
        return False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start: проверка капчи и выдача меню."""
    user = update.effective_user
    if not user:
        return

    # /start не режем антифлудом: иначе повторный старт молча пропадает
    # и кажется, что бот мёртвый.
    token = context.bot.token
    try:
        u_data = await get_or_create_user(user.id, source_bot_token=token)
    except Exception as e:
        logger.exception("start user load failed: %s", e)
        if update.effective_chat:
            try:
                await context.bot.send_message(update.effective_chat.id, "Не удалось открыть профиль. Нажмите /start ещё раз.")
            except Exception:
                pass
        return

    if update.message:
        try:
            await update.message.delete()
        except Exception:
            pass

    if not u_data.get("captcha_passed"):
        ok = await send_captcha_message(update, context)
        if not ok and update.effective_chat:
            try:
                await context.bot.send_message(
                    update.effective_chat.id,
                    "Не получилось показать капчу. Нажмите /start ещё раз.",
                )
            except Exception:
                pass
        return

    await show_main_menu(update, context)


async def captcha_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатия на вариант ответа капчи."""
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return

    try:
        ans = int(str(query.data).split("_", 1)[1])
    except Exception:
        await _answer_query(query, "Капча устарела")
        await send_captcha_message(update, context, is_wrong=True)
        return

    try:
        u_data = await get_or_create_user(user.id, source_bot_token=context.bot.token)
    except Exception as e:
        logger.exception("captcha user load failed: %s", e)
        await _answer_query(query, "Ошибка базы, нажмите ещё раз")
        return

    # Двойной клик по верному ответу не должен заново рисовать капчу
    if u_data.get("captcha_passed"):
        await _answer_query(query)
        context.user_data.pop("captcha_correct", None)
        pending_captcha.pop(user.id, None)
        try:
            if query.message and not query.message.photo:
                await query.message.delete()
        except Exception:
            pass
        await show_main_menu(update, context, force_new=True)
        return

    correct = context.user_data.get("captcha_correct")
    if correct is None:
        correct = pending_captcha.get(user.id)

    if correct is None:
        await _answer_query(query, "Капча устарела, вот новая")
        await send_captcha_message(update, context, is_wrong=True)
        return

    if int(correct) != ans:
        await _answer_query(query, "Неверно, попробуйте ещё раз")
        await send_captcha_message(update, context, is_wrong=True)
        return

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA busy_timeout=5000;")
            await db.execute("UPDATE users SET captcha_passed = 1 WHERE tg_id = ?", (user.id,))
            await db.commit()
    except Exception as e:
        logger.exception("captcha save failed: %s", e)
        await _answer_query(query, "Не удалось сохранить, нажмите ответ ещё раз")
        return

    await _answer_query(query, "Капча пройдена")
    context.user_data.pop("captcha_correct", None)
    pending_captcha.pop(user.id, None)
    context.user_data["menu_msg_id"] = None
    context.user_data["current_photo"] = None

    try:
        if query.message:
            await query.message.delete()
    except Exception:
        pass
    context.user_data.pop("captcha_msg_id", None)

    # Новое меню, без попытки превратить удалённый текст капчи в фото
    await show_main_menu(update, context, force_new=True)


async def show_main_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    force_new: bool = False,
):
    """Отображение главного меню с фото banner."""
    user = update.effective_user
    is_adm = user.id in ADMIN_IDS if user else False
    banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
    greeting = await get_config("greeting_text", DEFAULT_GREETING)
    kb = get_main_keyboard(is_admin=is_adm)
    await render_post(update, context, banner_photo, greeting, kb, force_new=force_new)

# ==============================================================================

# ==============================================================================
# ОБРАБОТЧИКИ: ПРОФИЛЬ И ПОПОЛНЕНИЕ КОШЕЛЬКА
# ==============================================================================

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Вкладка Профиль:
    ID Аккаунта: #F89JHT
    Баланс: {баланс} $
    Кнопки: Пополнить кошелек, Меню
    """
    user = update.effective_user
    u_data = await get_or_create_user(user.id)
    banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)

    caption = (
        f"<b>ID Аккаунта:</b> <code>{u_data['account_id']}</code>\n"
        f"<b>Баланс:</b> <code>{u_data['balance']:g} $</code>"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Пополнить кошелек", callback_data="deposit_start")],
        [get_back_button("nav_menu", "🔙 Меню")],
    ])
    await render_post(update, context, banner_photo, caption, kb)


async def deposit_start_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1 пополнения: запрос суммы."""
    banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
    caption = "Введите сумму пополнения:"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Отмена", callback_data="nav_profile")]
    ])
    context.user_data["state"] = "awaiting_deposit_amount"
    await render_post(update, context, banner_photo, caption, kb, keep_state=True)


async def deposit_show_requisites(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float):
    """
    Шаг 2 пополнения: точный текст реквизитов TRON (TRC20) и кнопка 'Я отправил'.
    """
    tron_address = await get_config("tron_address", DEFAULT_TRON_ADDRESS)
    banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)

    context.user_data["deposit_amount"] = amount

    caption = (
        "Пополнение осуществляется строго через перевод на TRON (TRC20), обязательно "
        "удостоверьтесь что вы пополняете именно через TRC20 в ином случае средства будут утеряны.\n"
        "TRON (TRC20)\n"
        f"<code>{tron_address}</code>\n\n"
        "Если вы не знаете как переводить с криптокошелька мы готовы вам помочь, для этого "
        "напишите в тех.поддержку. Так-же не нажимайте кнопку \"Я отправил\" если вы не совершали "
        "перевод, в ином случае вам могут заблокировать доступ к боту, нажимайте кнопку исключительно "
        "когда вы совершили перевод."
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Я отправил", callback_data="deposit_i_sent")],
        [InlineKeyboardButton("🔙 Отмена", callback_data="nav_profile")],
    ])
    await render_post(update, context, banner_photo, caption, kb, keep_state=True)


async def deposit_ask_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 3 пополнения: запрос скрина перевода."""
    banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
    caption = "📎 Пришлите скриншот с переводом:"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Отмена", callback_data="nav_profile")]
    ])
    context.user_data["state"] = "awaiting_deposit_screenshot"
    await render_post(update, context, banner_photo, caption, kb, keep_state=True)


# ==============================================================================
# ОБРАБОТЧИКИ: АССОРТИМЕНТ, ТОВАРЫ И ОТЗЫВЫ
# ==============================================================================

async def show_assortment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Вкладка Ассортимент:
    Прикрепленное фото: banneras.jpg
    Список товаров из БД.
    """
    banneras_photo = await get_config("banner_assort", FILE_BANNER_ASSORT)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, name, price FROM products ORDER BY id ASC")
        products = await cur.fetchall()

    buttons = []
    if not products:
        caption = "🛒 <b>Ассортимент товаров</b>\n\nВ данный момент товары отсутствуют."
    else:
        caption = "🛍️ <b>Ассортимент товаров</b>\n\nВыберите интересующий вас товар:"
        for pid, pname, pprice in products:
            buttons.append([InlineKeyboardButton(f"{pname} — {pprice:g} $", callback_data=f"prod_{pid}")])

    buttons.append([get_back_button("nav_menu", "🔙 Меню")])
    kb = InlineKeyboardMarkup(buttons)
    await render_post(update, context, banneras_photo, caption, kb)


async def show_product_card(update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int):
    """
    Карточка товара:
    Фото lir (для Конфет) или photo_path товара.
    Название, Описание, кнопки 'Купить', 'Отзывы', 'Назад'.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, name, description, price, photo_path FROM products WHERE id = ?", (product_id,))
        prod = await cur.fetchone()

    if not prod:
        await show_assortment(update, context)
        return

    pid, name, desc, price, photo_path = prod

    # Для Конфет явно используем lir.jpg согласно ТЗ
    if name.lower() == "конфеты" or not photo_path:
        photo = await get_config("photo_lir", FILE_PHOTO_LIR)
    else:
        photo = photo_path

    caption = (
        f"📦 <b>{name}</b>\n\n"
        f"{desc}\n\n"
        f"💰 <b>Цена:</b> <code>{price:g} $</code>"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Купить", callback_data=f"buy_{pid}")],
        [InlineKeyboardButton("⭐ Отзывы", callback_data=f"reviews_{pid}")],
        [InlineKeyboardButton("🔙 Назад в ассортимент", callback_data="nav_assortment")],
    ])
    await render_post(update, context, photo, caption, kb)


async def show_product_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int):
    """
    Отзывы о товаре:
    Показывает максимум 5 последних отзывов.
    Кнопка 'Добавить отзыв' и 'Назад'.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT name, photo_path FROM products WHERE id = ?", (product_id,))
        prod = await cur.fetchone()
        if not prod:
            await show_assortment(update, context)
            return
        pname, photo_path = prod

        cur = await db.execute(
            "SELECT user_account_id, text, created_at FROM reviews WHERE product_id = ? ORDER BY id DESC LIMIT 5",
            (product_id,),
        )
        revs = await cur.fetchall()

    photo = FILE_PHOTO_LIR if pname.lower() == "конфеты" else (photo_path or FILE_BANNER_MAIN)

    text_parts = [f"⭐ <b>Отзывы о товаре «{pname}»</b>\n"]
    if not revs:
        text_parts.append("<i>Пока нет ни одного отзыва. Вы можете быть первым!</i>")
    else:
        for aid, rtext, rdate in revs:
            # форматирование даты
            try:
                dt_obj = datetime.fromisoformat(rdate)
                dt_str = dt_obj.strftime("%d.%m.%Y %H:%M")
            except Exception:
                dt_str = str(rdate)
            text_parts.append(f"💬 <b>{aid}</b>: {rtext}\n📅 <i>{dt_str}</i>\n")

    caption = "\n".join(text_parts)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Написать отзыв", callback_data=f"addrev_{product_id}")],
        [InlineKeyboardButton("🔙 К товару", callback_data=f"prod_{product_id}")],
    ])
    await render_post(update, context, photo, caption, kb)


# ==============================================================================
# ОБРАБОТЧИКИ: ПОКУПКА И ЗАКАЗЫ
# ==============================================================================

async def buy_product_action(update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int):
    """
    Действие по кнопке 'Купить':
    - Проверка баланса.
    - Списание средств.
    - Создание заказа со статусом ожидания взятия админом.
    - Уведомление админам с кнопкой 'Взять заказ'.
    """
    user = update.effective_user
    u_data = await get_or_create_user(user.id)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT name, price, photo_path FROM products WHERE id = ?", (product_id,))
        prod = await cur.fetchone()

    if not prod:
        return

    pname, price, photo_path = prod
    photo = FILE_PHOTO_LIR if pname.lower() == "конфеты" else (photo_path or FILE_BANNER_MAIN)

    if u_data["balance"] < price:
        caption = (
            "недостаточно средств для покупки\n\n"
            f"Стоимость товара: <b>{price:g} $</b>\n"
            f"Ваш баланс: <b>{u_data['balance']:g} $</b>\n\n"
            "Пополните баланс в разделе Профиль."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Пополнить кошелек", callback_data="deposit_start")],
            [InlineKeyboardButton("🔙 К товару", callback_data=f"prod_{product_id}")],
        ])
        await render_post(update, context, photo, caption, kb)
        return

    # Списываем средства и создаем заказ
    token = context.bot.token
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = balance - ? WHERE tg_id = ?", (price, user.id))
        cur = await db.execute(
            "INSERT INTO orders (user_tg_id, product_id, price, status, taken_by_admin, source_bot_token, created_at) "
            "VALUES (?, ?, ?, 'pending_take', 0, ?, ?)",
            (user.id, product_id, price, token, now),
        )
        await db.commit()
        order_id = cur.lastrowid

    # Обновляем пост покупателя
    caption = (
        f"✅ <b>Заказ #{order_id} оформлен!</b>\n\n"
        f"Товар: <b>{pname}</b>\n"
        f"Сумма покупки: <b>{price:g} $</b> списана с баланса.\n\n"
        "⏳ Ожидайте, администратор скоро возьмется за ваш заказ."
    )
    kb = InlineKeyboardMarkup([
        [get_back_button("nav_menu", "🔙 Меню")]
    ])
    await render_post(update, context, photo, caption, kb)

    # Оповещение администраторам
    for adm_id in ADMIN_IDS:
        try:
            adm_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✋ Взять заказ", callback_data=f"order_take_{order_id}")]
            ])
            await context.bot.send_message(
                chat_id=adm_id,
                text=(
                    f"🛒 <b>Новый заказ #{order_id}!</b>\n\n"
                    f"Товар: <b>{pname}</b>\n"
                    f"Стоимость: <b>{price:g} $</b>\n"
                    f"Покупатель: <code>{u_data['account_id']}</code> (TG: <code>{user.id}</code>)\n"
                    f"Статус: Ожидает взятия в работу"
                ),
                reply_markup=adm_kb,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Could not notify admin {adm_id}: {e}")


# ==============================================================================
# ОБРАБОТЧИКИ: О НАС И ТЕХПОДДЕРЖКА
# ==============================================================================

async def show_about_us(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вкладка О нас (текст может изменять админ)."""
    banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
    about_text = await get_config("about_us", DEFAULT_ABOUT_US)
    kb = InlineKeyboardMarkup([
        [get_back_button("nav_menu", "🔙 Меню")]
    ])
    await render_post(update, context, banner_photo, about_text, kb)


async def show_support_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вкладка Тех.Поддержка."""
    banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
    caption = (
        "💬 <b>Служба технической поддержки</b>\n\n"
        "Если у вас возник вопрос по оплате, заказу или работе бота, "
        "вы можете задать его здесь, и администратор ответит вам."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Задать вопрос / Написать проблему", callback_data="sup_new")],
        [InlineKeyboardButton("📋 Мои обращения", callback_data="sup_my")],
        [get_back_button("nav_menu", "🔙 Меню")],
    ])
    await render_post(update, context, banner_photo, caption, kb)


async def show_my_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр тем обращений пользователя."""
    user = update.effective_user
    banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, subject, status, created_at FROM tickets WHERE user_tg_id = ? ORDER BY id DESC LIMIT 10",
            (user.id,),
        )
        ticks = await cur.fetchall()

    if not ticks:
        caption = "📋 <b>Ваши обращения</b>\n\nУ вас пока нет активных обращений в поддержку."
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✍️ Задать вопрос", callback_data="sup_new")],
            [InlineKeyboardButton("🔙 Назад", callback_data="nav_support")],
        ])
    else:
        caption = "📋 <b>Ваши обращения:</b>\nВыберите тему для просмотра переписки:"
        buttons = []
        for tid, subj, stat, c_at in ticks:
            st_icon = "🟢" if stat == "open" else "🔒"
            buttons.append([InlineKeyboardButton(f"{st_icon} Обращение #{tid} ({stat})", callback_data=f"sup_view_{tid}")])
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="nav_support")])
        kb = InlineKeyboardMarkup(buttons)

    await render_post(update, context, banner_photo, caption, kb)


async def view_ticket_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE, ticket_id: int):
    """Просмотр переписки по конкретному обращению."""
    user = update.effective_user
    banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, status FROM tickets WHERE id = ?", (ticket_id,))
        tick = await cur.fetchone()
        if not tick:
            await show_support_menu(update, context)
            return

        cur = await db.execute(
            "SELECT sender_role, text, created_at FROM ticket_messages WHERE ticket_id = ? ORDER BY id ASC",
            (ticket_id,),
        )
        messages = await cur.fetchall()

    status = tick[1]
    st_text = "🟢 Открыто" if status == "open" else "🔒 Закрыто"

    lines = [f"💬 <b>Обращение #{ticket_id}</b> [{st_text}]\n"]
    for role, mtext, mtime in messages:
        sender_lbl = "👤 Вы" if role == "user" else "👨‍💼 Поддержка"
        lines.append(f"{sender_lbl}: {mtext}")

    caption = "\n".join(lines)
    buttons = []
    if status == "open":
        buttons.append([InlineKeyboardButton("✉️ Ответить в тему", callback_data=f"sup_reply_{ticket_id}")])
    buttons.append([InlineKeyboardButton("🔙 К списку обращений", callback_data="sup_my")])
    kb = InlineKeyboardMarkup(buttons)
    await render_post(update, context, banner_photo, caption, kb)


# ==============================================================================
# ОБРАБОТЧИКИ: КОНСТРУКТОР БОТОВ (+2$, 10% РЕФЕРАЛКА)
# ==============================================================================

async def show_create_bot_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Вкладка 'Создать нашего бота':
    Инструкция, бонус 2$ за первого бота, 10% с пополнений.
    """
    banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
    user = update.effective_user
    u_data = await get_or_create_user(user.id)

    bonus_status = "✅ Получен" if u_data["bonus_claimed"] else "🎁 Доступен (2$ на баланс!)"

    caption = (
        "🤖 <b>Создайте своего бота и зарабатывайте!</b>\n\n"
        "Вы можете запустить собственного клона нашего магазина и получать пассивный доход!\n\n"
        f"🎁 <b>Бонус:</b> {bonus_status}\n"
        "За создание первого бота вам будет начислено <b>2$</b> на баланс (выдается строго 1 раз).\n\n"
        "💸 <b>Реферальный доход:</b>\n"
        "Вы будете получать <b>10%</b> от суммы каждого подтвержденного пополнения через вашего бота! "
        "Деньги сразу поступают на ваш баланс, и их можно тратить на любые покупки.\n\n"
        "<b>Инструкция по созданию:</b>\n"
        "1. Перейдите в официальный бот @BotFather\n"
        "2. Отправьте команду /newbot и следуйте подсказкам\n"
        "3. Скопируйте выданный API токен (например <code>123456789:ABCdef...</code>)\n"
        "4. Нажмите кнопку «Отправить токен бота» ниже и пришлите токен в чат."
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Отправить токен бота", callback_data="bot_token_input")],
        [InlineKeyboardButton("📋 Мои созданные боты", callback_data="my_bots_list")],
        [get_back_button("nav_menu", "🔙 Меню")],
    ])
    await render_post(update, context, banner_photo, caption, kb)


async def show_my_bots_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список ботов, созданных данным пользователем."""
    user = update.effective_user
    banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT bot_username, created_at FROM clone_bots WHERE owner_tg_id = ? ORDER BY id DESC",
            (user.id,),
        )
        bots = await cur.fetchall()

    if not bots:
        caption = "📋 <b>Ваши боты</b>\n\nВы еще не добавили ни одного бота."
    else:
        lines = ["📋 <b>Ваши подключенные боты:</b>\n"]
        for b_name, b_date in bots:
            lines.append(f"🤖 @{b_name} — активен (10% с пополнений)")
        caption = "\n".join(lines)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить еще бота", callback_data="bot_token_input")],
        [InlineKeyboardButton("🔙 Назад", callback_data="nav_create_bot")],
    ])
    await render_post(update, context, banner_photo, caption, kb)


# ==============================================================================
# ПАНЕЛЬ АДМИНИСТРАТОРА
# ==============================================================================

async def admin_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главный экран админ-панели."""
    user = update.effective_user
    if not user or user.id not in ADMIN_IDS:
        if update.callback_query:
            try:
                await update.callback_query.answer("Нет доступа", show_alert=True)
            except Exception:
                pass
        return

    banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
    caption = "⚙️ <b>Панель администратора Pharmojelly</b>\n\nВыберите нужный раздел управления:"

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📦 Заказы", callback_data="adm_orders"),
            InlineKeyboardButton("💰 Баланс", callback_data="adm_balance"),
        ],
        [
            InlineKeyboardButton("🛒 Товары", callback_data="adm_products"),
            InlineKeyboardButton("⭐ Отзывы", callback_data="adm_reviews"),
        ],
        [
            InlineKeyboardButton("🖼️ Сменить фото", callback_data="adm_photos"),
            InlineKeyboardButton("💳 Реквизиты", callback_data="adm_requisites"),
        ],
        [
            InlineKeyboardButton("💬 Поддержка", callback_data="adm_support"),
            InlineKeyboardButton("📝 О нас", callback_data="adm_about"),
        ],
        [InlineKeyboardButton("📢 Массовая рассылка", callback_data="adm_broadcast")],
        [get_back_button("nav_menu", "🔙 В клиентское меню")],
    ])
    await render_post(update, context, banner_photo, caption, kb)


# ==============================================================================
# ОБРАБОТЧИК КЛИКОВ ПО КНОПКАМ (CALLBACK QUERY ROUTER)
# ==============================================================================

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Единый маршрутизатор всех нажатий inline-кнопок."""
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return

    data = query.data or ""

    # Капча обрабатывается ДО антифлуда.
    # Иначе первый клик сразу после /start съедается, кнопка «умирает»,
    # а в логе нет нормальной ошибки.
    if data.startswith("captcha_"):
        await captcha_callback(update, context)
        return

    # Защита от флуда
    if not check_anti_flood(user.id):
        try:
            await query.answer("Не кликайте слишком быстро", show_alert=False)
        except Exception:
            pass
        return

    logger.info(f"Callback from user {user.id}: {data}")

    # Защита: пока капча не пройдена, никакой другой функционал не доступен
    u_data = await get_or_create_user(user.id, source_bot_token=context.bot.token)
    if not u_data.get("captcha_passed"):
        try:
            await query.answer("⚠️ Сначала решите пример для доступа к боту!", show_alert=True)
        except Exception:
            pass
        await send_captcha_message(update, context)
        return

    # 2. Главная навигация
    if data == "nav_menu":
        await show_main_menu(update, context)
        return
    if data == "nav_profile":
        await show_profile(update, context)
        return
    if data == "nav_assortment":
        await show_assortment(update, context)
        return
    if data == "nav_about":
        await show_about_us(update, context)
        return
    if data == "nav_support":
        await show_support_menu(update, context)
        return
    if data == "nav_create_bot":
        await show_create_bot_menu(update, context)
        return

    # 3. Пополнение кошелька
    if data == "deposit_start":
        await deposit_start_prompt(update, context)
        return
    if data == "deposit_i_sent":
        await deposit_ask_screenshot(update, context)
        return

    # 4. Просмотр товара
    if data.startswith("prod_"):
        pid = int(data.split("_")[1])
        await show_product_card(update, context, pid)
        return

    # 5. Купить товар
    if data.startswith("buy_"):
        pid = int(data.split("_")[1])
        await buy_product_action(update, context, pid)
        return

    # 6. Отзывы
    if data.startswith("reviews_"):
        pid = int(data.split("_")[1])
        await show_product_reviews(update, context, pid)
        return
    if data.startswith("addrev_"):
        pid = int(data.split("_")[1])
        context.user_data["review_prod_id"] = pid
        context.user_data["state"] = "awaiting_user_review"
        banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
        caption = "✍️ <b>Напишите ваш отзыв:</b>\nОтправьте текст отзыва в чат:"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Отмена", callback_data=f"reviews_{pid}")]
        ])
        await render_post(update, context, banner_photo, caption, kb, keep_state=True)
        return

    # 7. Поддержка (клиент)
    if data == "sup_new":
        context.user_data["state"] = "awaiting_support_message"
        banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
        caption = "✍️ <b>Опишите вашу проблему или вопрос:</b>\nОтправьте сообщение в чат:"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Отмена", callback_data="nav_support")]
        ])
        await render_post(update, context, banner_photo, caption, kb, keep_state=True)
        return
    if data == "sup_my":
        await show_my_tickets(update, context)
        return
    if data.startswith("sup_view_"):
        tid = int(data.split("_")[2])
        await view_ticket_dialog(update, context, tid)
        return
    if data.startswith("sup_reply_"):
        tid = int(data.split("_")[2])
        context.user_data["reply_ticket_id"] = tid
        context.user_data["state"] = "awaiting_ticket_reply"
        banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
        caption = f"✍️ <b>Введите ваш ответ по обращению #{tid}:</b>"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Отмена", callback_data=f"sup_view_{tid}")]
        ])
        await render_post(update, context, banner_photo, caption, kb, keep_state=True)
        return

    # 8. Создание бота
    if data == "bot_token_input":
        context.user_data["state"] = "awaiting_bot_token"
        banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
        caption = (
            "🤖 <b>Отправьте API токен вашего бота</b>\n\n"
            "Скопируйте токен из @BotFather и пришлите его сюда в чат:"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Отмена", callback_data="nav_create_bot")]
        ])
        await render_post(update, context, banner_photo, caption, kb, keep_state=True)
        return
    if data == "my_bots_list":
        await show_my_bots_list(update, context)
        return

    # 9. Ответ покупателя на заказ
    if data.startswith("buyer_msg_"):
        oid = int(data.split("_")[2])
        context.user_data["buyer_order_id"] = oid
        context.user_data["state"] = "awaiting_buyer_order_msg"
        banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
        caption = f"✍️ <b>Введите сообщение для администратора по заказу #{oid}:</b>"
        kb = InlineKeyboardMarkup([
            [get_back_button("nav_menu", "🔙 Отмена")]
        ])
        await render_post(update, context, banner_photo, caption, kb, keep_state=True)
        return

    # ==========================================================================
    # ДЕЙСТВИЯ АДМИНИСТРАТОРА (ДОСТУПНО ТОЛЬКО ДЛЯ ADMIN_IDS)
    # ==========================================================================
    if user.id in ADMIN_IDS:
        if data == "admin_home":
            await admin_home(update, context)
            return

        # Действия по заказу (Взять заказ, Подтвердить, Отклонить, Написать сообщение)
        if data.startswith("order_take_"):
            oid = int(data.split("_")[2])
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT status, user_tg_id, product_id, price FROM orders WHERE id = ?", (oid,))
                ord_row = await cur.fetchone()
                if not ord_row:
                    await query.answer("Заказ не найден!", show_alert=True)
                    return
                if ord_row[0] != "pending_take":
                    await query.answer("Этот заказ уже взят или обработан другим администратором!", show_alert=True)
                    return
                await db.execute("UPDATE orders SET status = 'in_progress', taken_by_admin = ? WHERE id = ?", (user.id, oid))
                await db.commit()
                buyer_tg_id = ord_row[1]

            # Уведомляем покупателя строго по ТЗ
            try:
                await context.bot.send_message(buyer_tg_id, "Админ взялся за ваш заказ")
            except Exception as e:
                logger.warning(f"Failed to message buyer: {e}")

            # Меняем кнопки у админа на 3 обязательные кнопки
            adm_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Подтвердить заказ", callback_data=f"order_accept_{oid}"),
                    InlineKeyboardButton("❌ Отклонить заказ", callback_data=f"order_reject_{oid}"),
                ],
                [InlineKeyboardButton("✉️ Написать сообщение", callback_data=f"order_msg_{oid}")],
            ])
            try:
                await query.edit_message_text(
                    f"🛒 <b>Заказ #{oid} взят вами в работу!</b>\n\n"
                    f"Покупателю отправлено оповещение: «Админ взялся за ваш заказ».\n"
                    f"Выберите следующее действие:",
                    reply_markup=adm_kb,
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

        if data.startswith("order_msg_"):
            oid = int(data.split("_")[2])
            context.user_data["admin_msg_order_id"] = oid
            context.user_data["state"] = "awaiting_admin_order_msg"
            try:
                await query.message.reply_text(
                    f"✍️ <b>Введите текст сообщения для покупателя по заказу #{oid}:</b>",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Отмена", callback_data="admin_home")]
                    ]),
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

        if data.startswith("order_accept_"):
            oid = int(data.split("_")[2])
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT user_tg_id FROM orders WHERE id = ?", (oid,))
                r = await cur.fetchone()
                if r:
                    buyer_id = r[0]
                    await db.execute("UPDATE orders SET status = 'confirmed' WHERE id = ?", (oid,))
                    await db.commit()
                    # Уведомление покупателю строго по ТЗ
                    try:
                        await context.bot.send_message(buyer_id, "Админ подтвердил ваш заказ")
                    except Exception as e:
                        logger.warning(f"Error messaging buyer: {e}")
            try:
                await query.edit_message_text(f"✅ <b>Заказ #{oid} успешно подтвержден!</b>", parse_mode="HTML")
            except Exception:
                pass
            return

        if data.startswith("order_reject_"):
            oid = int(data.split("_")[2])
            price = 0.0
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT user_tg_id, price FROM orders WHERE id = ?", (oid,))
                r = await cur.fetchone()
                if not r:
                    try:
                        await query.answer("Заказ не найден", show_alert=True)
                    except Exception:
                        pass
                    return
                buyer_id, price = r
                await db.execute("UPDATE orders SET status = 'declined' WHERE id = ?", (oid,))
                await db.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (price, buyer_id))
                await db.commit()
            try:
                await context.bot.send_message(
                    buyer_id,
                    "Ваш заказ отклонен админом. Деньги отправлены на ваш счет",
                )
            except Exception as e:
                logger.warning(f"Error messaging buyer: {e}")
            try:
                await query.edit_message_text(
                    f"❌ <b>Заказ #{oid} отклонен.</b>\nСумма {price:g} $ возвращена на счет покупателя.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

        # Открытие подтвержденных заказов админом
        if data == "adm_confirmed_orders":
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "SELECT o.id, p.name, u.account_id FROM orders o "
                    "JOIN products p ON o.product_id = p.id "
                    "JOIN users u ON o.user_tg_id = u.tg_id "
                    "WHERE o.status = 'confirmed' ORDER BY o.id DESC LIMIT 15"
                )
                conf_orders = await cur.fetchall()

            if not conf_orders:
                caption = "📦 <b>Подтвержденные заказы</b>\n\nПодтвержденных заказов пока нет."
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="adm_orders")]])
            else:
                caption = "📦 <b>Подтвержденные заказы:</b>\nНажмите на заказ, чтобы открыть его и написать покупателю:"
                buttons = []
                for co_id, co_name, co_acc in conf_orders:
                    buttons.append([InlineKeyboardButton(f"Заказ #{co_id} | {co_name} ({co_acc})", callback_data=f"adm_open_conf_{co_id}")])
                buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="adm_orders")])
                kb = InlineKeyboardMarkup(buttons)
            banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
            await render_post(update, context, banner_photo, caption, kb)
            return

        if data.startswith("adm_open_conf_"):
            oid = int(data.split("_")[3])
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT user_tg_id FROM orders WHERE id = ?", (oid,))
                r = await cur.fetchone()
                if r:
                    buyer_id = r[0]
                    # Уведомляем покупателя строго по ТЗ: "Админ открыл ваш заказ"
                    try:
                        await context.bot.send_message(buyer_id, "Админ открыл ваш заказ")
                    except Exception as e:
                        logger.warning(f"Error notifying buyer: {e}")

            context.user_data["admin_msg_order_id"] = oid
            context.user_data["state"] = "awaiting_admin_order_msg"
            banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
            caption = (
                f"📂 <b>Заказ #{oid} открыт!</b>\n\n"
                "Покупателю отправлено уведомление «Админ открыл ваш заказ».\n"
                "Введите сообщение для покупателя в чат:"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Отмена", callback_data="adm_confirmed_orders")]
            ])
            await render_post(update, context, banner_photo, caption, kb, keep_state=True)
            return

        # Действия по депозитам (Принять, Отклонить)
        if data.startswith("dep_accept_"):
            dep_id = int(data.split("_")[2])
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "SELECT user_tg_id, amount, check_id, status, source_bot_token FROM deposits WHERE id = ?",
                    (dep_id,),
                )
                dep = await cur.fetchone()
                if not dep:
                    await query.answer("Пополнение не найдено!", show_alert=True)
                    return
                u_id, amt, chk, stat, src_bot = dep
                if stat != "pending":
                    await query.answer("Это пополнение уже обработано!", show_alert=True)
                    return

                await db.execute("UPDATE deposits SET status = 'accepted' WHERE id = ?", (dep_id,))
                await db.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (amt, u_id))

                # Реферальное начисление (10% создателю реферального бота)
                ref_bonus = round(amt * 0.10, 2)
                ref_owner_id = None
                if src_bot:
                    c = await db.execute("SELECT owner_tg_id FROM clone_bots WHERE token = ?", (src_bot,))
                    owner_row = await c.fetchone()
                    if owner_row and owner_row[0] != u_id:
                        ref_owner_id = owner_row[0]
                        await db.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (ref_bonus, ref_owner_id))

                await db.commit()

            # Сообщение покупателю об успешном пополнении
            try:
                await context.bot.send_message(
                    u_id,
                    f"✅ <b>Сообщение об успешном пополнении!</b>\n"
                    f"Сумма: <b>{amt:g} $</b> зачислена на ваш баланс.\n"
                    f"Чек: <code>{chk}</code>",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"Error messaging deposit user: {e}")

            # Оповещение рефоводу (владельцу бота)
            if ref_owner_id:
                try:
                    await context.bot.send_message(
                        ref_owner_id,
                        f"💰 <b>Реферальный бонус!</b>\n\n"
                        f"Пользователь пополнил баланс через вашего бота на сумму {amt:g} $.\n"
                        f"Вам начислено 10%: <b>+{ref_bonus:g} $</b> на баланс!",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            try:
                await query.edit_message_caption(
                    caption=f"✅ <b>Пополнение #{dep_id} принято!</b>\nНачислено: {amt:g} $. Чек: {chk}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

        if data.startswith("dep_reject_"):
            dep_id = int(data.split("_")[2])
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT user_tg_id, check_id, status FROM deposits WHERE id = ?", (dep_id,))
                dep = await cur.fetchone()
                if not dep:
                    await query.answer("Пополнение не найдено!", show_alert=True)
                    return
                u_id, chk, stat = dep
                if stat != "pending":
                    await query.answer("Это пополнение уже обработано!", show_alert=True)
                    return

                await db.execute("UPDATE deposits SET status = 'rejected' WHERE id = ?", (dep_id,))
                await db.commit()

            # Сообщение покупателю строго по ТЗ
            try:
                await context.bot.send_message(
                    u_id,
                    "Ваше пополнение отклонено, если вы совершали перевод то напишите в Тех.Поддержку.",
                )
            except Exception as e:
                logger.warning(f"Error messaging rejected user: {e}")

            try:
                await query.edit_message_caption(
                    caption=f"❌ <b>Пополнение #{dep_id} отклонено.</b> Чек: {chk}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

        # Разделы админки
        if data == "adm_orders":
            banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
            caption = "📦 <b>Управление заказами</b>\n\nВыберите действие:"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Подтвержденные заказы", callback_data="adm_confirmed_orders")],
                [InlineKeyboardButton("🔙 В админ-меню", callback_data="admin_home")],
            ])
            await render_post(update, context, banner_photo, caption, kb)
            return

        if data == "adm_balance":
            context.user_data["state"] = "adm_give_balance_account"
            banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
            caption = (
                "💰 <b>Выдача баланса пользователю</b>\n\n"
                "Введите уникальный ID аккаунта (например <code>#F89JHT</code>):"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="admin_home")]])
            await render_post(update, context, banner_photo, caption, kb, keep_state=True)
            return

        if data == "adm_products":
            banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
            caption = "🛒 <b>Управление товарами</b>\n\nВыберите действие:"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Добавить товар", callback_data="adm_add_product")],
                [InlineKeyboardButton("🗑️ Удалить товар", callback_data="adm_del_product_list")],
                [InlineKeyboardButton("🔙 В админ-меню", callback_data="admin_home")],
            ])
            await render_post(update, context, banner_photo, caption, kb)
            return

        if data == "adm_add_product":
            context.user_data["state"] = "adm_add_prod_name"
            banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
            caption = "➕ <b>Добавление нового товара</b>\n\nВведите название товара:"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="adm_products")]])
            await render_post(update, context, banner_photo, caption, kb, keep_state=True)
            return

        if data == "adm_del_product_list":
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT id, name FROM products ORDER BY id ASC")
                prods = await cur.fetchall()
            banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
            if not prods:
                caption = "🛒 Нет доступных товаров для удаления."
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="adm_products")]])
            else:
                caption = "🗑️ <b>Выберите товар для удаления:</b>"
                buttons = [[InlineKeyboardButton(f"❌ {pname}", callback_data=f"adm_do_del_prod_{pid}")] for pid, pname in prods]
                buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="adm_products")])
                kb = InlineKeyboardMarkup(buttons)
            await render_post(update, context, banner_photo, caption, kb)
            return

        if data.startswith("adm_do_del_prod_"):
            pid = int(data.split("_")[4])
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM products WHERE id = ?", (pid,))
                await db.execute("DELETE FROM reviews WHERE product_id = ?", (pid,))
                await db.commit()
            await query.answer("✅ Товар удален!", show_alert=True)
            await admin_home(update, context)
            return

        if data == "adm_reviews":
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT id, name FROM products ORDER BY id ASC")
                prods = await cur.fetchall()
            banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
            if not prods:
                caption = "⭐ Нет товаров для добавления отзыва."
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В админ-меню", callback_data="admin_home")]])
            else:
                caption = "⭐ <b>Выберите товар для добавления отзыва:</b>"
                buttons = [[InlineKeyboardButton(pname, callback_data=f"adm_add_rev_{pid}")] for pid, pname in prods]
                buttons.append([InlineKeyboardButton("🔙 В админ-меню", callback_data="admin_home")])
                kb = InlineKeyboardMarkup(buttons)
            await render_post(update, context, banner_photo, caption, kb)
            return

        if data.startswith("adm_add_rev_"):
            pid = int(data.split("_")[3])
            context.user_data["adm_rev_prod_id"] = pid
            context.user_data["state"] = "adm_rev_text"
            banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
            caption = "⭐ <b>Введите текст отзыва:</b>"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="adm_reviews")]])
            await render_post(update, context, banner_photo, caption, kb, keep_state=True)
            return

        if data == "adm_photos":
            banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
            caption = (
                "🖼️ <b>Смена фотографий на любых постах</b>\n\n"
                "Выберите, какое изображение вы хотите заменить:"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🖼️ Главный баннер (banner.jpg)", callback_data="adm_change_photo_banner")],
                [InlineKeyboardButton("🛍️ Баннер ассортимента (banneras.jpg)", callback_data="adm_change_photo_banneras")],
                [InlineKeyboardButton("🍬 Фото конфет (lir.jpg)", callback_data="adm_change_photo_lir")],
                [InlineKeyboardButton("📦 Фото товара (выбрать)", callback_data="adm_change_photo_product")],
                [InlineKeyboardButton("🔙 В админ-меню", callback_data="admin_home")],
            ])
            await render_post(update, context, banner_photo, caption, kb)
            return

        if data.startswith("adm_change_photo_"):
            target = data.replace("adm_change_photo_", "")
            if target == "product":
                async with aiosqlite.connect(DB_PATH) as db:
                    cur = await db.execute("SELECT id, name FROM products ORDER BY id ASC")
                    prods = await cur.fetchall()
                banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
                caption = "Выберите товар для смены фотографии:"
                buttons = [[InlineKeyboardButton(pname, callback_data=f"adm_ch_prod_photo_{pid}")] for pid, pname in prods]
                buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="adm_photos")])
                kb = InlineKeyboardMarkup(buttons)
                await render_post(update, context, banner_photo, caption, kb)
                return
            else:
                context.user_data["photo_target"] = target
                context.user_data["state"] = "awaiting_new_photo"
                banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
                caption = f"🖼️ Отправьте новую фотографию для <b>{target}</b> в чат:"
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="adm_photos")]])
                await render_post(update, context, banner_photo, caption, kb, keep_state=True)
                return

        if data.startswith("adm_ch_prod_photo_"):
            pid = int(data.split("_")[4])
            context.user_data["photo_target_prod_id"] = pid
            context.user_data["state"] = "awaiting_new_prod_photo"
            banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
            caption = "🖼️ Отправьте новую фотографию для этого товара в чат:"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="adm_photos")]])
            await render_post(update, context, banner_photo, caption, kb, keep_state=True)
            return

        if data == "adm_requisites":
            current_tron = await get_config("tron_address", DEFAULT_TRON_ADDRESS)
            context.user_data["state"] = "awaiting_new_tron_address"
            banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
            caption = (
                f"💳 <b>Реквизиты оплаты (TRON TRC20)</b>\n\n"
                f"Текущий адрес:\n<code>{current_tron}</code>\n\n"
                "Отправьте новый адрес кошелька в чат для изменения:"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="admin_home")]])
            await render_post(update, context, banner_photo, caption, kb, keep_state=True)
            return

        if data == "adm_about":
            current_about = await get_config("about_us", DEFAULT_ABOUT_US)
            context.user_data["state"] = "awaiting_new_about"
            banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
            caption = (
                "📝 <b>Редактирование раздела «О нас»</b>\n\n"
                "Текущий текст:\n\n"
                f"{current_about}\n\n"
                "Отправьте новый текст в чат:"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="admin_home")]])
            await render_post(update, context, banner_photo, caption, kb, keep_state=True)
            return

        if data == "adm_broadcast":
            context.user_data["state"] = "awaiting_broadcast_message"
            banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
            caption = (
                "📢 <b>Массовая рассылка всем пользователям</b>\n\n"
                "Отправьте текст рассылки (или фото с подписью). "
                "Рассылка будет отправлена всем пользователям основного и реферальных ботов с защитой от банов и лимитов."
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="admin_home")]])
            await render_post(update, context, banner_photo, caption, kb, keep_state=True)
            return

        if data == "adm_support":
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "SELECT t.id, t.status, u.account_id FROM tickets t "
                    "JOIN users u ON t.user_tg_id = u.tg_id "
                    "WHERE t.status = 'open' ORDER BY t.id DESC LIMIT 15"
                )
                ticks = await cur.fetchall()

            banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
            if not ticks:
                caption = "💬 <b>Тех.Поддержка (Администратор)</b>\n\nНет открытых вопросов от пользователей."
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В админ-меню", callback_data="admin_home")]])
            else:
                caption = "💬 <b>Открытые вопросы в тех.поддержку:</b>"
                buttons = []
                for tid, tstat, tacc in ticks:
                    buttons.append([InlineKeyboardButton(f"Тикет #{tid} от {tacc}", callback_data=f"adm_ticket_view_{tid}")])
                buttons.append([InlineKeyboardButton("🔙 В админ-меню", callback_data="admin_home")])
                kb = InlineKeyboardMarkup(buttons)
            await render_post(update, context, banner_photo, caption, kb)
            return

        if data.startswith("adm_ticket_view_"):
            tid = int(data.split("_")[3])
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT user_tg_id, status FROM tickets WHERE id = ?", (tid,))
                tick = await cur.fetchone()
                if not tick:
                    await query.answer("Тикет не найден!", show_alert=True)
                    return
                u_id, stat = tick
                cur = await db.execute(
                    "SELECT sender_role, text FROM ticket_messages WHERE ticket_id = ? ORDER BY id ASC",
                    (tid,),
                )
                msgs = await cur.fetchall()

            lines = [f"💬 <b>Обращение #{tid}</b> (Пользователь: {u_id})\n"]
            for role, txt in msgs:
                lbl = "👤 Клиент" if role == "user" else "👨‍💼 Админ"
                lines.append(f"{lbl}: {txt}")

            caption = "\n".join(lines)
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✉️ Ответить", callback_data=f"adm_ticket_reply_{tid}"),
                    InlineKeyboardButton("🔒 Закрыть вопрос", callback_data=f"adm_ticket_close_{tid}"),
                ],
                [InlineKeyboardButton("🔙 Назад", callback_data="adm_support")],
            ])
            banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
            await render_post(update, context, banner_photo, caption, kb)
            return

        if data.startswith("adm_ticket_reply_"):
            tid = int(data.split("_")[3])
            context.user_data["adm_reply_tid"] = tid
            context.user_data["state"] = "awaiting_adm_ticket_reply"
            banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)
            caption = f"✍️ <b>Введите ваш ответ на обращение #{tid}:</b>"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data=f"adm_ticket_view_{tid}")]])
            await render_post(update, context, banner_photo, caption, kb, keep_state=True)
            return

        if data.startswith("adm_ticket_close_"):
            tid = int(data.split("_")[3])
            now = datetime.now(timezone.utc).isoformat()
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE tickets SET status = 'closed', closed_at = ? WHERE id = ?", (now, tid))
                await db.commit()
            await query.answer("🔒 Вопрос закрыт!", show_alert=True)
            await admin_home(update, context)
            return


# ==============================================================================
# ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ И ВВОДА
# ==============================================================================

async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработка текстового ввода пользователя.
    Удаляет сообщение пользователя и обновляет предыдущий пост бота с фото.
    """
    user = update.effective_user
    if not user:
        return

    # Защита капчей: пока капча не пройдена, текстовый ввод недоступен
    u_data = await get_or_create_user(user.id, source_bot_token=context.bot.token)
    if not u_data.get("captcha_passed"):
        if update.message:
            try:
                await update.message.delete()
            except Exception:
                pass
        await send_captcha_message(update, context)
        return

    text = update.message.text.strip()
    state = context.user_data.get("state")
    banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)

    # 1. Ввод суммы пополнения
    if state == "awaiting_deposit_amount":
        try:
            amount = float(text.replace(",", "."))
            if amount <= 0:
                raise ValueError
        except ValueError:
            caption = "❗ <b>Некорректная сумма.</b>\nВведите положительное число (например, <code>50</code>):"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="nav_profile")]])
            await render_post(update, context, banner_photo, caption, kb, keep_state=True)
            return

        context.user_data.pop("state", None)
        await deposit_show_requisites(update, context, amount)
        return

    # 2. Пользователь пишет отзыв
    if state == "awaiting_user_review":
        pid = context.user_data.get("review_prod_id")
        if not pid:
            await show_main_menu(update, context)
            return
        u_data = await get_or_create_user(user.id)
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO reviews (product_id, user_tg_id, user_account_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
                (pid, user.id, u_data["account_id"], text[:300], now),
            )
            # Оставляем максимум 5 последних отзывов, старые удаляем согласно ТЗ
            cur = await db.execute("SELECT id FROM reviews WHERE product_id = ? ORDER BY id DESC", (pid,))
            all_ids = [r[0] for r in await cur.fetchall()]
            if len(all_ids) > 5:
                to_delete = all_ids[5:]
                for del_id in to_delete:
                    await db.execute("DELETE FROM reviews WHERE id = ?", (del_id,))
            await db.commit()

        context.user_data.pop("state", None)
        caption = "✅ <b>Спасибо! Ваш отзыв опубликован.</b>"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К товару", callback_data=f"prod_{pid}")]])
        await render_post(update, context, banner_photo, caption, kb)
        return

    # 3. Пользователь создает обращение в техподдержку
    if state == "awaiting_support_message":
        u_data = await get_or_create_user(user.id)
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO tickets (user_tg_id, message, subject, status, created_at) VALUES (?, ?, 'Вопрос', 'open', ?)",
                (user.id, text, now),
            )
            ticket_id = cur.lastrowid
            await db.execute(
                "INSERT INTO ticket_messages (ticket_id, sender_tg_id, sender_role, text, created_at) "
                "VALUES (?, ?, 'user', ?, ?)",
                (ticket_id, user.id, text, now),
            )
            await db.commit()

        context.user_data.pop("state", None)
        # Ответ пользователю строго по ТЗ: "Ваш вопрос был отправлен."
        caption = "Ваш вопрос был отправлен."
        kb = InlineKeyboardMarkup([[get_back_button("nav_menu", "🔙 Меню")]])
        await render_post(update, context, banner_photo, caption, kb)

        # Оповещаем админов
        for adm_id in ADMIN_IDS:
            try:
                adm_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✉️ Ответить", callback_data=f"adm_ticket_reply_{ticket_id}")],
                    [InlineKeyboardButton("🔒 Закрыть вопрос", callback_data=f"adm_ticket_close_{ticket_id}")],
                ])
                await context.bot.send_message(
                    chat_id=adm_id,
                    text=(
                        f"💬 <b>Новый вопрос в тех.поддержку #{ticket_id}!</b>\n\n"
                        f"Пользователь: <code>{u_data['account_id']}</code> (TG: {user.id})\n"
                        f"Текст:\n{text}"
                    ),
                    reply_markup=adm_kb,
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"Error alerting admin {adm_id}: {e}")
        return

    # 4. Ответ пользователя в существующий тикет
    if state == "awaiting_ticket_reply":
        tid = context.user_data.get("reply_ticket_id")
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO ticket_messages (ticket_id, sender_tg_id, sender_role, text, created_at) "
                "VALUES (?, ?, 'user', ?, ?)",
                (tid, user.id, text, now),
            )
            await db.commit()

        context.user_data.pop("state", None)
        caption = "✅ <b>Ваш ответ отправлен в поддержку!</b>"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К обращению", callback_data=f"sup_view_{tid}")]])
        await render_post(update, context, banner_photo, caption, kb)

        # Оповещаем админов
        for adm_id in ADMIN_IDS:
            try:
                adm_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✉️ Ответить", callback_data=f"adm_ticket_reply_{tid}")],
                    [InlineKeyboardButton("🔒 Закрыть вопрос", callback_data=f"adm_ticket_close_{tid}")],
                ])
                await context.bot.send_message(
                    chat_id=adm_id,
                    text=f"💬 <b>Новый ответ от пользователя по тикету #{tid}:</b>\n\n{text}",
                    reply_markup=adm_kb,
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    # 5. Покупатель отвечает на сообщение по заказу
    if state == "awaiting_buyer_order_msg":
        oid = context.user_data.get("buyer_order_id")
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO order_messages (order_id, sender_tg_id, sender_role, text, created_at) "
                "VALUES (?, ?, 'buyer', ?, ?)",
                (oid, user.id, text, now),
            )
            cur = await db.execute("SELECT taken_by_admin FROM orders WHERE id = ?", (oid,))
            r = await cur.fetchone()
            admin_to_notify = r[0] if (r and r[0]) else list(ADMIN_IDS)[0]
            await db.commit()

        context.user_data.pop("state", None)
        caption = "✅ <b>Ваше сообщение передано администратору!</b>"
        kb = InlineKeyboardMarkup([[get_back_button("nav_menu", "🔙 Меню")]])
        await render_post(update, context, banner_photo, caption, kb)

        # Доставляем админу сообщение с кнопками подтверждения/отклонения/ответа
        adm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Подтвердить заказ", callback_data=f"order_accept_{oid}"),
                InlineKeyboardButton("❌ Отклонить заказ", callback_data=f"order_reject_{oid}"),
            ],
            [InlineKeyboardButton("✉️ Написать сообщение", callback_data=f"order_msg_{oid}")],
        ])
        try:
            await context.bot.send_message(
                chat_id=admin_to_notify,
                text=f"✉️ <b>Сообщение от покупателя по заказу #{oid}:</b>\n\n{text}",
                reply_markup=adm_kb,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Error notifying admin: {e}")
        return

    # 6. Пользователь вводит токен для создания клона бота
    if state == "awaiting_bot_token":
        token_candidate = text.strip()
        # Проверка формата
        if not re.match(r"^\d{8,11}:[a-zA-Z0-9_-]{35}$", token_candidate):
            caption = (
                "❌ <b>Неверный формат токена!</b>\n\n"
                "Токен от @BotFather должен иметь вид: <code>123456789:ABCdef...</code>\n"
                "Попробуйте отправить токен снова:"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="nav_create_bot")]])
            await render_post(update, context, banner_photo, caption, kb, keep_state=True)
            return

        # Проверка, не добавлен ли уже этот токен
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT id FROM clone_bots WHERE token = ?", (token_candidate,))
            if await cur.fetchone():
                caption = "⚠️ <b>Этот бот уже добавлен в систему!</b>"
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="nav_create_bot")]])
                await render_post(update, context, banner_photo, caption, kb)
                return

        # Валидация токена через Telegram API
        try:
            temp_app = Application.builder().token(token_candidate).build()
            bot_info = await temp_app.bot.get_me()
        except Exception as e:
            logger.warning(f"Failed to validate token {token_candidate}: {e}")
            caption = (
                "❌ <b>Не удалось авторизовать бота!</b>\n\n"
                "Убедитесь, что токен скопирован корректно и бот активен в @BotFather."
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="nav_create_bot")]])
            await render_post(update, context, banner_photo, caption, kb, keep_state=True)
            return

        # Начисление бонуса 2$ (строго 1 раз согласно ТЗ)
        now = datetime.now(timezone.utc).isoformat()
        u_data = await get_or_create_user(user.id)
        bonus_text = ""
        async with aiosqlite.connect(DB_PATH) as db:
            if not u_data["bonus_claimed"]:
                await db.execute("UPDATE users SET balance = balance + 2.0, bonus_claimed = 1 WHERE tg_id = ?", (user.id,))
                bonus_text = "\n\n🎉 <b>Вам начислен бонус 2.00 $ на баланс за создание первого бота!</b>"
            else:
                bonus_text = "\n\nℹ️ <i>(Бонус 2$ уже был начислен вам ранее за первого бота)</i>"

            await db.execute(
                "INSERT INTO clone_bots (token, owner_tg_id, bot_username, created_at, is_active) "
                "VALUES (?, ?, ?, ?, 1)",
                (token_candidate, user.id, bot_info.username, now),
            )
            await db.commit()

        # Динамический запуск бота в текущем цикле событий
        asyncio.create_task(start_clone_bot(token_candidate))

        context.user_data.pop("state", None)
        caption = (
            f"✅ <b>Бот @{bot_info.username} успешно создан и запущен!</b>"
            f"{bonus_text}\n\n"
            "Все пользователи вашего бота будут приносить вам <b>10%</b> с каждого подтвержденного пополнения!"
        )
        kb = InlineKeyboardMarkup([[get_back_button("nav_menu", "🔙 Меню")]])
        await render_post(update, context, banner_photo, caption, kb)
        return

    # ==========================================================================
    # ТЕКСТОВЫЕ ДЕЙСТВИЯ АДМИНИСТРАТОРА
    # ==========================================================================
    if user.id in ADMIN_IDS:
        # Сообщение админа покупателю по заказу
        if state == "awaiting_admin_order_msg":
            oid = context.user_data.get("admin_msg_order_id")
            now = datetime.now(timezone.utc).isoformat()
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT user_tg_id FROM orders WHERE id = ?", (oid,))
                r = await cur.fetchone()
                if not r:
                    await render_post(update, context, banner_photo, "Заказ не найден.", get_main_keyboard(True))
                    return
                buyer_id = r[0]
                await db.execute(
                    "INSERT INTO order_messages (order_id, sender_tg_id, sender_role, text, created_at) "
                    "VALUES (?, ?, 'admin', ?, ?)",
                    (oid, user.id, text, now),
                )
                await db.commit()

            context.user_data.pop("state", None)

            # Доставляем покупателю строго по ТЗ
            # "Когда админ пишет сообщение то оно приходит покупателю в такой форме: Админ ответил на ваш заказ: (и снизу должен уже быть текст)"
            buyer_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✉️ Ответить админу", callback_data=f"buyer_msg_{oid}")]
            ])
            try:
                await context.bot.send_message(
                    chat_id=buyer_id,
                    text=f"Админ ответил на ваш заказ:\n{text}",
                    reply_markup=buyer_kb,
                )
            except Exception as e:
                logger.warning(f"Error sending message to buyer: {e}")

            # Подтверждение админу
            adm_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Подтвердить заказ", callback_data=f"order_accept_{oid}"),
                    InlineKeyboardButton("❌ Отклонить заказ", callback_data=f"order_reject_{oid}"),
                ],
                [InlineKeyboardButton("✉️ Написать еще сообщение", callback_data=f"order_msg_{oid}")],
                [InlineKeyboardButton("🔙 В админ-меню", callback_data="admin_home")],
            ])
            caption = f"✅ <b>Сообщение отправлено покупателю по заказу #{oid}!</b>"
            await render_post(update, context, banner_photo, caption, adm_kb)
            return

        # Ответ админа на тикет техподдержки
        if state == "awaiting_adm_ticket_reply":
            tid = context.user_data.get("adm_reply_tid")
            now = datetime.now(timezone.utc).isoformat()
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT user_tg_id FROM tickets WHERE id = ?", (tid,))
                r = await cur.fetchone()
                if not r:
                    return
                u_id = r[0]
                await db.execute(
                    "INSERT INTO ticket_messages (ticket_id, sender_tg_id, sender_role, text, created_at) "
                    "VALUES (?, ?, 'admin', ?, ?)",
                    (tid, user.id, text, now),
                )
                await db.commit()

            context.user_data.pop("state", None)

            # Сообщение покупателю строго по ТЗ
            # "Когда админ ответит на вопрос то пользователю прийдет сообщение
            #  Тех.Поддержка ответила на вашу проблему/вопрос чтобы посмотреть откройте вкладку Тех.Поддержка и выберите тему"
            try:
                await context.bot.send_message(
                    chat_id=u_id,
                    text="Тех.Поддержка ответила на вашу проблему/вопрос чтобы посмотреть откройте вкладку Тех.Поддержка и выберите тему",
                )
            except Exception as e:
                logger.warning(f"Error messaging ticket user: {e}")

            caption = f"✅ <b>Ответ на обращение #{tid} отправлен!</b>"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔒 Закрыть вопрос", callback_data=f"adm_ticket_close_{tid}")],
                [InlineKeyboardButton("🔙 В меню поддержки", callback_data="adm_support")],
            ])
            await render_post(update, context, banner_photo, caption, kb)
            return

        # Выдача баланса: Шаг 1 (ID аккаунта)
        if state == "adm_give_balance_account":
            account_id = text.strip().upper()
            if not account_id.startswith("#"):
                account_id = "#" + account_id

            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT tg_id, balance FROM users WHERE account_id = ?", (account_id,))
                row = await cur.fetchone()

            if not row:
                caption = f"❌ Пользователь с ID <code>{account_id}</code> не найден!\nПопробуйте снова:"
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="admin_home")]])
                await render_post(update, context, banner_photo, caption, kb, keep_state=True)
                return

            context.user_data["target_user_tg_id"] = row[0]
            context.user_data["target_account_id"] = account_id
            context.user_data["state"] = "adm_give_balance_amount"
            caption = (
                f"👤 Пользователь <code>{account_id}</code> найден.\n"
                f"Текущий баланс: <b>{row[1]:g} $</b>\n\n"
                "Введите сумму для начисления (можно с минусом, например <code>100</code> или <code>-50</code>):"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="admin_home")]])
            await render_post(update, context, banner_photo, caption, kb, keep_state=True)
            return

        # Выдача баланса: Шаг 2 (Сумма)
        if state == "adm_give_balance_amount":
            try:
                amount = float(text.replace(",", "."))
            except ValueError:
                caption = "❗ Введите число (например <code>100</code>):"
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="admin_home")]])
                await render_post(update, context, banner_photo, caption, kb, keep_state=True)
                return

            u_tg_id = context.user_data["target_user_tg_id"]
            u_acc = context.user_data["target_account_id"]

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (amount, u_tg_id))
                cur = await db.execute("SELECT balance FROM users WHERE tg_id = ?", (u_tg_id,))
                new_bal = (await cur.fetchone())[0]
                await db.commit()

            context.user_data.pop("state", None)

            # Оповещаем пользователя
            try:
                sign = "+" if amount > 0 else ""
                await context.bot.send_message(
                    chat_id=u_tg_id,
                    text=f"💰 <b>Изменение баланса!</b>\nАдминистратор изменил ваш баланс на <b>{sign}{amount:g} $</b>.\nНовый баланс: <b>{new_bal:g} $</b>",
                    parse_mode="HTML",
                )
            except Exception:
                pass

            caption = (
                f"✅ <b>Баланс успешно обновлен!</b>\n\n"
                f"Пользователь: <code>{u_acc}</code>\n"
                f"Сумма: <b>{amount:g} $</b>\n"
                f"Новый баланс: <b>{new_bal:g} $</b>"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В админ-меню", callback_data="admin_home")]])
            await render_post(update, context, banner_photo, caption, kb)
            return

        # Добавление товара: Шаг 1 (Имя)
        if state == "adm_add_prod_name":
            context.user_data["new_prod_name"] = text
            context.user_data["state"] = "adm_add_prod_desc"
            caption = f"Товар: <b>{text}</b>\n\nТеперь введите описание товара:"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="adm_products")]])
            await render_post(update, context, banner_photo, caption, kb, keep_state=True)
            return

        # Добавление товара: Шаг 2 (Описание)
        if state == "adm_add_prod_desc":
            context.user_data["new_prod_desc"] = text
            context.user_data["state"] = "adm_add_prod_price"
            caption = "Введите цену товара (число в $):"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="adm_products")]])
            await render_post(update, context, banner_photo, caption, kb, keep_state=True)
            return

        # Добавление товара: Шаг 3 (Цена)
        if state == "adm_add_prod_price":
            try:
                price = float(text.replace(",", "."))
                if price <= 0:
                    raise ValueError
            except ValueError:
                caption = "❗ Введите положительное число:"
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="adm_products")]])
                await render_post(update, context, banner_photo, caption, kb, keep_state=True)
                return

            context.user_data["new_prod_price"] = price
            context.user_data["state"] = "adm_add_prod_photo"
            caption = (
                f"Цена: <b>{price:g} $</b>\n\n"
                "Теперь <b>отправьте фото</b> товара в чат (или напишите <code>skip</code> для стандартного баннера):"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="adm_products")]])
            await render_post(update, context, banner_photo, caption, kb, keep_state=True)
            return

        # Добавление товара: Шаг 4 (Фото через skip)
        if state == "adm_add_prod_photo" and text.lower() == "skip":
            pname = context.user_data["new_prod_name"]
            pdesc = context.user_data["new_prod_desc"]
            pprice = context.user_data["new_prod_price"]
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT INTO products (name, description, price, photo_path) VALUES (?, ?, ?, ?)",
                    (pname, pdesc, pprice, FILE_BANNER_MAIN),
                )
                await db.commit()
            context.user_data.pop("state", None)
            caption = f"✅ Товар «<b>{pname}</b>» успешно добавлен!"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню товаров", callback_data="adm_products")]])
            await render_post(update, context, banner_photo, caption, kb)
            return

        # Добавление отзыва админом: Шаг 1 (Текст)
        if state == "adm_rev_text":
            context.user_data["adm_rev_text"] = text
            context.user_data["state"] = "adm_rev_date"
            caption = (
                "⭐ <b>Введите дату для отзыва:</b>\n\n"
                "Вы можете указать любую дату (например, <code>2026-11-20 18:30</code> или <code>now</code> для текущей):"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="adm_reviews")]])
            await render_post(update, context, banner_photo, caption, kb, keep_state=True)
            return

        # Добавление отзыва админом: Шаг 2 (Любая дата по ТЗ)
        if state == "adm_rev_date":
            pid = context.user_data.get("adm_rev_prod_id")
            rtext = context.user_data.get("adm_rev_text")
            date_input = text.strip()

            if date_input.lower() == "now":
                created_at = datetime.now(timezone.utc).isoformat()
            else:
                # Админ может ввести любую дату
                created_at = date_input

            u_data = await get_or_create_user(user.id)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT INTO reviews (product_id, user_tg_id, user_account_id, text, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (pid, user.id, u_data["account_id"], rtext, created_at),
                )
                # Оставляем максимум 5 последних отзывов
                cur = await db.execute("SELECT id FROM reviews WHERE product_id = ? ORDER BY id DESC", (pid,))
                all_ids = [r[0] for r in await cur.fetchall()]
                if len(all_ids) > 5:
                    to_delete = all_ids[5:]
                    for del_id in to_delete:
                        await db.execute("DELETE FROM reviews WHERE id = ?", (del_id,))
                await db.commit()

            context.user_data.pop("state", None)
            caption = "✅ <b>Отзыв с заданной датой успешно добавлен!</b>"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В админ-меню", callback_data="admin_home")]])
            await render_post(update, context, banner_photo, caption, kb)
            return

        # Смена реквизитов TRON (TRC20)
        if state == "awaiting_new_tron_address":
            new_addr = text.strip()
            await set_config("tron_address", new_addr)
            context.user_data.pop("state", None)
            caption = f"✅ <b>Реквизиты успешно обновлены!</b>\n\nНовый адрес:\n<code>{new_addr}</code>"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В админ-меню", callback_data="admin_home")]])
            await render_post(update, context, banner_photo, caption, kb)
            return

        # Смена текста "О нас"
        if state == "awaiting_new_about":
            await set_config("about_us", text)
            context.user_data.pop("state", None)
            caption = "✅ <b>Раздел «О нас» успешно обновлен!</b>"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В админ-меню", callback_data="admin_home")]])
            await render_post(update, context, banner_photo, caption, kb)
            return

        # Массовая рассылка текстом
        if state == "awaiting_broadcast_message":
            context.user_data.pop("state", None)
            asyncio.create_task(run_broadcast(context.bot, text=text))
            caption = "🚀 <b>Массовая рассылка запущена в фоновом режиме!</b>"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В админ-меню", callback_data="admin_home")]])
            await render_post(update, context, banner_photo, caption, kb)
            return

    # Если текста нет в состояниях — показываем главное меню
    await show_main_menu(update, context)


# ==============================================================================
# ОБРАБОТЧИК ФОТО И СКРИНШОТОВ
# ==============================================================================

async def photo_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработка входящих фотографий:
    - Скриншоты перевода (депозиты).
    - Фото новых товаров от администратора.
    - Смена фото постов/товаров.
    - Массовая рассылка с фото.
    """
    user = update.effective_user
    if not user or not update.message.photo:
        return

    # Защита капчей: пока капча не пройдена, отправка фото недоступна
    u_data = await get_or_create_user(user.id, source_bot_token=context.bot.token)
    if not u_data.get("captcha_passed"):
        try:
            await update.message.delete()
        except Exception:
            pass
        await send_captcha_message(update, context)
        return

    photo_file = update.message.photo[-1]
    state = context.user_data.get("state")
    banner_photo = await get_config("banner_main", FILE_BANNER_MAIN)

    # 1. Загрузка скриншота пополнения
    if state == "awaiting_deposit_screenshot":
        amount = context.user_data.get("deposit_amount", 0.0)
        u_data = await get_or_create_user(user.id)
        check_id = gen_check_id()
        now = datetime.now(timezone.utc).isoformat()
        token = context.bot.token

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO deposits (user_tg_id, amount, check_id, photo_file_id, status, source_bot_token, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (user.id, amount, check_id, photo_file.file_id, token, now),
            )
            deposit_id = cur.lastrowid
            await db.commit()

        context.user_data.pop("state", None)

        # Сообщение пользователю строго по ТЗ:
        # "Ваше пополнение принято в обработку и будет зачисленно через некоторое время. Чек: (Тут должен быть уникальный чек для каждого пополнения)"
        caption = f"Ваше пополнение принято в обработку и будет зачисленно через некоторое время. Чек: {check_id}"
        kb = InlineKeyboardMarkup([[get_back_button("nav_menu", "🔙 Меню")]])
        await render_post(update, context, banner_photo, caption, kb)

        # Оповещение администраторам прямо в боте с прикрепленным скрином
        adm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Принять", callback_data=f"dep_accept_{deposit_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"dep_reject_{deposit_id}"),
            ]
        ])
        for adm_id in ADMIN_IDS:
            try:
                await context.bot.send_photo(
                    chat_id=adm_id,
                    photo=photo_file.file_id,
                    caption=(
                        f"💰 <b>Новое пополнение!</b>\n\n"
                        f"Сумма: <b>{amount:g} $</b>\n"
                        f"Чек: <code>{check_id}</code>\n"
                        f"Пользователь: <code>{u_data['account_id']}</code> (TG: {user.id})"
                    ),
                    reply_markup=adm_kb,
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"Failed to alert admin {adm_id} about deposit: {e}")
        return

    # Действия администратора с фото
    if user.id in ADMIN_IDS:
        # Добавление фото нового товара
        if state == "adm_add_prod_photo":
            pname = context.user_data["new_prod_name"]
            pdesc = context.user_data["new_prod_desc"]
            pprice = context.user_data["new_prod_price"]

            # Сохраняем фото на диск
            file_obj = await photo_file.get_file()
            save_name = f"prod_{uuid.uuid4().hex[:8]}.jpg"
            save_path = get_abs_path(save_name)
            await file_obj.download_to_drive(save_path)

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT INTO products (name, description, price, photo_path) VALUES (?, ?, ?, ?)",
                    (pname, pdesc, pprice, save_name),
                )
                await db.commit()

            context.user_data.pop("state", None)
            caption = f"✅ Товар «<b>{pname}</b>» с фотографией успешно добавлен!"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню товаров", callback_data="adm_products")]])
            await render_post(update, context, banner_photo, caption, kb)
            return

        # Смена фото поста (banner, banneras, lir)
        if state == "awaiting_new_photo":
            target = context.user_data.get("photo_target")
            file_obj = await photo_file.get_file()

            filename_map = {
                "banner": FILE_BANNER_MAIN,
                "banneras": FILE_BANNER_ASSORT,
                "lir": FILE_PHOTO_LIR,
            }
            fname = filename_map.get(target, f"{target}.jpg")
            save_path = get_abs_path(fname)
            await file_obj.download_to_drive(save_path)

            if target == "banner":
                await set_config("banner_main", fname)
            elif target == "banneras":
                await set_config("banner_assort", fname)
            elif target == "lir":
                await set_config("photo_lir", fname)

            context.user_data.pop("state", None)
            caption = f"✅ Фотография для <b>{target}</b> успешно обновлена!"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В админ-меню", callback_data="admin_home")]])
            await render_post(update, context, fname, caption, kb)
            return

        # Смена фото конкретного товара
        if state == "awaiting_new_prod_photo":
            pid = context.user_data.get("photo_target_prod_id")
            file_obj = await photo_file.get_file()
            save_name = f"prod_{uuid.uuid4().hex[:8]}.jpg"
            save_path = get_abs_path(save_name)
            await file_obj.download_to_drive(save_path)

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE products SET photo_path = ? WHERE id = ?", (save_name, pid))
                await db.commit()

            context.user_data.pop("state", None)
            caption = "✅ Фотография товара успешно обновлена!"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В админ-меню", callback_data="admin_home")]])
            await render_post(update, context, save_name, caption, kb)
            return

        # Рассылка с фото
        if state == "awaiting_broadcast_message":
            caption_text = update.message.caption or ""
            context.user_data.pop("state", None)
            asyncio.create_task(run_broadcast(context.bot, photo_file_id=photo_file.file_id, text=caption_text))
            caption = "🚀 <b>Массовая рассылка с фото запущена!</b>"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В админ-меню", callback_data="admin_home")]])
            await render_post(update, context, banner_photo, caption, kb)
            return

    # Удаляем сообщение и возвращаем меню
    try:
        await update.message.delete()
    except Exception:
        pass


# ==============================================================================
# МАССОВАЯ РАССЫЛКА И ФОНОВЫЕ ЗАДАЧИ
# ==============================================================================

async def run_broadcast(bot, text: str = "", photo_file_id: str = ""):
    """
    Массовая рассылка всем пользователям бота (включая пользователей реферальных ботов).
    Защита от лимитов Telegram: задержка 0.05 с, обработка RetryAfter и Forbidden.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT DISTINCT tg_id FROM users")
        users = [r[0] for r in await cur.fetchall()]

    success = 0
    fail = 0

    for uid in users:
        try:
            if photo_file_id:
                await bot.send_photo(chat_id=uid, photo=photo_file_id, caption=text, parse_mode="HTML")
            else:
                await bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
            success += 1
        except tg_error.RetryAfter as ra:
            await asyncio.sleep(ra.retry_after)
            try:
                if photo_file_id:
                    await bot.send_photo(chat_id=uid, photo=photo_file_id, caption=text, parse_mode="HTML")
                else:
                    await bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
                success += 1
            except Exception:
                fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)

    # Отчет админам
    for adm_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=adm_id,
                text=f"📢 <b>Рассылка завершена!</b>\n\nУспешно доставлено: {success}\nОшибок/заблокировано: {fail}",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def cleanup_old_tickets_job():
    """
    Фоновая задача очистки закрытых тикетов старше 5 дней согласно ТЗ:
    'закрытые вопросы хранятся 5 дней потом удаляются'
    """
    while True:
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "DELETE FROM ticket_messages WHERE ticket_id IN "
                    "(SELECT id FROM tickets WHERE status = 'closed' AND closed_at IS NOT NULL AND closed_at < ?)",
                    (cutoff,),
                )
                await db.execute(
                    "DELETE FROM tickets WHERE status = 'closed' AND closed_at IS NOT NULL AND closed_at < ?",
                    (cutoff,),
                )
                await db.commit()
        except Exception as e:
            logger.error(f"Error in cleanup_old_tickets_job: {e}")
        # Проверка раз в час
        await asyncio.sleep(3600)


# ==============================================================================
# ФАБРИКА БОТОВ И МУЛЬТИБОТ-АРХИТЕКТУРА
# ==============================================================================

def _make_request() -> HTTPXRequest:
    """Отдельный HTTP-клиент. Один и тот же объект на polling и send вешает бота."""
    kwargs = {
        "connect_timeout": 30.0,
        "read_timeout": 30.0,
        "write_timeout": 30.0,
        "pool_timeout": 30.0,
        "connection_pool_size": 8,
    }
    if not PROXY_URL:
        return HTTPXRequest(**kwargs)
    # PTB 20: proxy_url, PTB 21+: proxy
    try:
        return HTTPXRequest(proxy=PROXY_URL, **kwargs)
    except TypeError:
        return HTTPXRequest(proxy_url=PROXY_URL, **kwargs)


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Любая ошибка хендлера больше не оставляет вечный спиннер на кнопке."""
    logger.exception("Handler error: %s", context.error)
    if isinstance(update, Update) and update.callback_query:
        try:
            await update.callback_query.answer("Ошибка, попробуйте ещё раз", show_alert=False)
        except Exception:
            pass


def build_application(token: str) -> Application:
    """Создает и настраивает экземпляр Application со всеми хендлерами."""
    builder = (
        Application.builder()
        .token(token)
        .request(_make_request())
        .get_updates_request(_make_request())
    )

    if BASE_URL:
        url = BASE_URL.rstrip("/")
        if not url.endswith("/bot"):
            url = f"{url}/bot"
        builder = builder.base_url(url)

    app = builder.build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", admin_home))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.PHOTO, photo_message_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))
    app.add_error_handler(global_error_handler)

    return app


async def start_clone_bot(token: str):
    """Асинхронный запуск клонированного реферального бота в фоне."""
    if token in running_applications:
        return
    try:
        app = build_application(token)
        await app.initialize()
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=False,
            allowed_updates=["message", "callback_query"],
        )
        running_applications[token] = app
        bot_user = await app.bot.get_me()
        logger.info(f"Clone bot @{bot_user.username} started successfully.")
    except Exception as e:
        logger.error(f"Failed to start clone bot with token {token}: {e}")


# ==============================================================================
# ТОЧКА ВХОДА (MAIN)
# ==============================================================================

async def main_async():
    """Главная асинхронная функция запуска."""
    global main_app_instance

    if not BOT_TOKEN:
        print("Нет BOT_TOKEN. Создайте файл .env рядом со скриптом и впишите строку:")
        print("BOT_TOKEN=123456789:AA...")
        print("Старый токен из кода лучше перевыпустить в @BotFather: он уже был в исходнике.")
        sys.exit(1)

    logger.info("Initializing Pharmojelly Bot Database...")
    await init_db()

    # Запуск фонового клинера тикетов (удаление через 5 дней)
    asyncio.create_task(cleanup_old_tickets_job())

    # Создание и запуск основного бота
    logger.info("Starting main bot...")
    main_app = build_application(BOT_TOKEN)
    main_app_instance = main_app

    try:
        await main_app.initialize()
        await main_app.start()
        await main_app.updater.start_polling(
            drop_pending_updates=False,
            allowed_updates=["message", "callback_query"],
        )
        running_applications[BOT_TOKEN] = main_app
        main_info = await main_app.bot.get_me()
        logger.info(f"Main bot @{main_info.username} is RUNNING!")
    except Exception as exc:
        logger.error(f"Failed to connect to Telegram API: {exc}")
        name = type(exc).__name__
        text = str(exc)
        print("\n" + "=" * 65)
        if "Unauthorized" in name or "Unauthorized" in text or "Rejected" in name:
            print("НЕВЕРНЫЙ BOT_TOKEN.")
            print("Откройте @BotFather, сделайте /revoke и впишите новый токен в .env:")
            print("BOT_TOKEN=123456789:AA...")
        elif any(s in name for s in ("TimedOut", "NetworkError", "ConnectError")) or "timeout" in text.lower() or "Connect" in text:
            print("НЕТ СВЯЗИ С api.telegram.org (таймаут или блок провайдера).")
            print("1. Включите VPN.")
            print("2. Либо пропишите в .env прокси: PROXY_URL=socks5://127.0.0.1:10808")
            print("3. Либо зеркало: BASE_URL=https://<зеркало>/bot")
        else:
            print("БОТ НЕ СТАРТАНУЛ.")
            print(f"{name}: {text}")
        print("=" * 65 + "\n")
        sys.exit(1)

    # Запуск всех ранее сохраненных реферальных ботов из БД
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT token FROM clone_bots WHERE is_active = 1")
        tokens = [r[0] for r in await cur.fetchall()]

    for c_token in tokens:
        asyncio.create_task(start_clone_bot(c_token))

    print(f"\n=======================================================")
    print(f"🚀 Pharmojelly Bot успешно запущен: @{main_info.username}")
    print(f"👑 Администраторы: {ADMIN_IDS}")
    print(f"🤖 Запущено реферальных ботов: {len(tokens)}")
    print(f"=======================================================\n")

    # Бесконечный цикл поддержания работы
    try:
        while True:
            await asyncio.sleep(1)
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Shutting down bots...")
        for tok, app in running_applications.items():
            try:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
            except Exception:
                pass


def main():
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nБот остановлен пользователем.")


if __name__ == "__main__":
    main()
