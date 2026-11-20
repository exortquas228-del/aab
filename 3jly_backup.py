#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram-бот «Pharmojelly» — полная версия.
"""

import asyncio
import logging
import uuid
import os
from datetime import datetime, timedelta

import aiosqlite
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    InputFile,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    JobQueue,
    MessageHandler,
    filters,
)

BOT_TOKEN = "9223853959:AAGnEmmssJrArVQvygb_00_QrnME9DeRasg"
ADMIN_IDS = {8822479121, 8818045521}

BANNER_DEFAULT = "banner.jpg"
BANNER_ASSORT = "banneras.jpg"

DB_PATH = "shop.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (tg_id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, balance INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT, photo_path TEXT);
            CREATE TABLE IF NOT EXISTS reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL, user_tg_id INTEGER NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_tg_id INTEGER NOT NULL, product_id INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, admin_note TEXT);
            CREATE TABLE IF NOT EXISTS deposits (id INTEGER PRIMARY KEY AUTOINCREMENT, user_tg_id INTEGER NOT NULL, amount INTEGER NOT NULL, check_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, user_tg_id INTEGER NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, closed_at TEXT);
            CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        await db.commit()


def gen_account_id():
    return "#" + uuid.uuid4().hex[:6].upper()


def make_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton("🛍️ Ассортимент")],
            [KeyboardButton("👤 Профиль"), KeyboardButton("ℹ️ О нас")],
            [KeyboardButton("💬 Тех.поддержка")],
        ],
        resize_keyboard=True,
    )


def make_admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton("📦 Управление заказами")],
            [KeyboardButton("💰 Управление балансом")],
            [KeyboardButton("🛒 Управление товарами")],
            [KeyboardButton("📢 Массовая рассылка")],
            [KeyboardButton("⚙️ Настройки оплаты")],
        ],
        resize_keyboard=True,
    )


def load_image(path):
    return InputFile(path)


async def is_admin(tg_id):
    return tg_id in ADMIN_IDS


# states
(
    CAPTCHA, MAIN_MENU, SELECT_PRODUCT, PRODUCT_DETAIL,
    DEPOSIT_AMOUNT, DEPOSIT_PROOF, SUPPORT_MESSAGE,
    ADMIN_ORDER_ACTION, ADMIN_DEPOSIT_ACTION, BROADCAST_TEXT,
    ADMIN_ADD_PRODUCT_NAME, ADMIN_ADD_PRODUCT_DESC, ADMIN_ADD_PRODUCT_PHOTO,
    ADMIN_GIVE_BALANCE_ID, ADMIN_GIVE_BALANCE_AMOUNT,
    ADMIN_REVIEW_PRODUCT, ADMIN_REVIEW_TEXT, ADMIN_REVIEW_DATE,
) = range(18)


async def start(update, context):
    chat_id = update.effective_chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT tg_id FROM users WHERE tg_id = ?", (chat_id,))
        if not await cur.fetchone():
            await db.execute("INSERT INTO users (tg_id, account_id, balance) VALUES (?, ?, 0)", (chat_id, gen_account_id()))
            await db.commit()
    a, b = uuid.uuid4().int % 10, uuid.uuid4().int % 10
    context.user_data["captcha_answer"] = a + b
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(str(a + b), callback_data="captcha_ok"), InlineKeyboardButton(str(a + b + 1), callback_data="captcha_bad")]])
    await update.message.reply_photo(photo=load_image(BANNER_DEFAULT), caption=f"👋 Привет! Пройди капчу.\nСколько будет {a} + {b}?", reply_markup=kb)
    return CAPTCHA


async def captcha_handler(update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "captcha_ok":
        await query.edit_message_caption(caption="✅ Капча пройдена! Добро пожаловать.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Перейти в меню", callback_data="show_menu")]]))
        return MAIN_MENU
    await query.edit_message_caption(caption="❌ Неверно. Попробуйте /start ещё раз.")
    return ConversationHandler.END


async def show_main_menu(update, context):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("📋 Главное меню", reply_markup=make_main_keyboard())
    return MAIN_MENU


async def profile(update, context):
    chat_id = update.effective_chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT account_id, balance FROM users WHERE tg_id = ?", (chat_id,))
        row = await cur.fetchone()
    if not row:
        await update.message.reply_text("❗ Пользователь не найден.")
        return
    account_id, balance = row
    await update.message.reply_photo(photo=load_image(BANNER_DEFAULT), caption=f"🆔 ID аккаунта: {account_id}\n💰 Баланс: {balance} ₽", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("🔙 Меню")]], resize_keyboard=True))


async def support_start(update, context):
    await update.message.reply_text("🛠️ Опишите свою проблему.")
    return SUPPORT_MESSAGE


async def support_receive(update, context):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO tickets (user_tg_id, message, status, created_at) VALUES (?,?, 'open', ?)", (chat_id, text, datetime.utcnow().isoformat()))
        await db.commit()
    await update.message.reply_text("✅ Ваш вопрос был отправлен.")
    for admin_id in ADMIN_IDS:
        await context.bot.send_message(admin_id, f"🆕 Новый запрос от {chat_id}:\n\n{text}")
    return MAIN_MENU


async def show_assortment(update, context):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, name FROM products")
        rows = await cur.fetchall()
    if not rows:
        await update.message.reply_text("🛒 Товаров пока нет.")
        return MAIN_MENU
    buttons = [[InlineKeyboardButton(name, callback_data=f"product_{pid}")] for pid, name in rows]
    await update.message.reply_photo(photo=load_image(BANNER_ASSORT), caption="📦 Выберите товар:", reply_markup=InlineKeyboardMarkup(buttons))
    return SELECT_PRODUCT


async def product_detail(update, context):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT name, description, photo_path FROM products WHERE id = ?", (prod_id,))
        row = await cur.fetchone()
    if not row:
        await query.message.reply_text("❗ Товар не найден.")
        return SELECT_PRODUCT
    name, descr, photo = row
    caption = f"*{name}*\n\n{descr}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Купить", callback_data=f"buy_{prod_id}"), InlineKeyboardButton("🗣️ Отзывы", callback_data=f"reviews_{prod_id}")],
        [InlineKeyboardButton("🔙 Вернуться", callback_data="back_to_assort")],
    ])
    await query.message.reply_photo(photo=load_image(photo) if photo and os.path.isfile(photo) else load_image(BANNER_DEFAULT), caption=caption, parse_mode="Markdown", reply_markup=kb)
    return PRODUCT_DETAIL


async def back_to_assort(update, context):
    query = update.callback_query
    await query.answer()
    return await show_assortment(update, context)


async def show_reviews(update, context):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT r.text, u.account_id, r.created_at FROM reviews r JOIN users u ON r.user_tg_id = u.tg_id WHERE r.product_id = ? ORDER BY r.created_at DESC LIMIT 5", (prod_id,))
        rows = await cur.fetchall()
    if not rows:
        text = "📭 Пока нет отзывов."
    else:
        text = "\n\n".join(f"💬 {rev}\n🆔 {aid}\n🕒 {datetime.fromisoformat(dt).strftime('%Y-%m-%d %H:%M')}" for rev, aid, dt in rows)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✍️ Добавить отзыв", callback_data=f"addreview_{prod_id}")], [InlineKeyboardButton("🔙 Вернуться", callback_data=f"product_{prod_id}")]])
    await query.message.reply_text(text, reply_markup=kb)
    return PRODUCT_DETAIL


async def add_review_prompt(update, context):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[1])
    context.user_data["review_product_id"] = prod_id
    await query.message.reply_text("🖊️ Отправьте ваш отзыв (до 300 символов).")
    return PRODUCT_DETAIL


async def add_review_save(update, context):
    chat_id = update.effective_chat.id
    prod_id = context.user_data.get("review_product_id")
    if not prod_id:
        await update.message.reply_text("❗ Ошибка.")
        return PRODUCT_DETAIL
    review_text = update.message.text.strip()
    if len(review_text) > 300:
        await update.message.reply_text("❗ Слишком длинный отзыв.")
        return PRODUCT_DETAIL
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO reviews (product_id, user_tg_id, text, created_at) VALUES (?,?,?,?)", (prod_id, chat_id, review_text, datetime.utcnow().isoformat()))
        await db.commit()
    await update.message.reply_text("✅ Отзыв сохранён.")
    for admin_id in ADMIN_IDS:
        await context.bot.send_message(admin_id, f"🆕 Новый отзыв на товар {prod_id} от {chat_id}:\n\n{review_text}")
    return PRODUCT_DETAIL


async def buy_product_start(update, context):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[1])
    chat_id = query.message.chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT name, description FROM products WHERE id = ?", (prod_id,))
        row = await cur.fetchone()
        if not row:
            await query.message.reply_text("❗ Товар не найден.")
            return PRODUCT_DETAIL
        name, descr = row
        price = 0
        for part in descr.split():
            if part.lower().startswith("price:"):
                try:
                    price = int(part.split(":")[1])
                except ValueError:
                    pass
        cur = await db.execute("SELECT balance FROM users WHERE tg_id = ?", (chat_id,))
        bal_row = await cur.fetchone()
        balance = bal_row[0] if bal_row else 0
    if price == 0:
        await query.message.reply_text("⚠️ У товара нет цены (price:123 в описании).")
        return PRODUCT_DETAIL
    if balance < price:
        await query.message.reply_text(f"❌ Недостаточно средств. Стоимость: {price} ₽, баланс: {balance} ₽.")
        return PRODUCT_DETAIL
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO orders (user_tg_id, product_id, status, created_at) VALUES (?,?, 'pending_admin', ?)", (chat_id, prod_id, datetime.utcnow().isoformat()))
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        order_id = (await cur.fetchone())[0]
        await db.execute("UPDATE users SET balance = balance - ? WHERE tg_id = ?", (price, chat_id))
        await db.commit()
    for admin_id in ADMIN_IDS:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить", callback_data=f"order_accept_{order_id}"), InlineKeyboardButton("❌ Отклонить", callback_data=f"order_reject_{order_id}")],
            [InlineKeyboardButton("✍️ Написать сообщение", callback_data=f"order_msg_{order_id}")],
        ])
        await context.bot.send_message(admin_id, f"🛒 Новый заказ №{order_id}\nПользователь: {chat_id}\nТовар: {name}\nСтоимость: {price} ₽", reply_markup=kb)
    await query.message.reply_text("✅ Заказ создан. Ожидайте подтверждения.")
    return PRODUCT_DETAIL


async def admin_order_action(update, context):
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id
    if admin_id not in ADMIN_IDS:
        await query.message.reply_text("❌ Нет прав.")
        return
    data = query.data
    if data.startswith("order_accept_"):
        order_id = int(data.split("_")[2])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE orders SET status = 'approved' WHERE id = ?", (order_id,))
            await db.commit()
            cur = await db.execute("SELECT user_tg_id FROM orders WHERE id = ?", (order_id,))
            user_tg_id = (await cur.fetchone())[0]
        await query.message.reply_text(f"✅ Заказ #{order_id} подтверждён.")
        await context.bot.send_message(user_tg_id, f"✅ Администратор подтвердил ваш заказ #{order_id}.")
    elif data.startswith("order_reject_"):
        order_id = int(data.split("_")[2])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE orders SET status = 'declined' WHERE id = ?", (order_id,))
            await db.commit()
            cur = await db.execute("SELECT user_tg_id, product_id FROM orders WHERE id = ?", (order_id,))
            user_id, prod_id = await cur.fetchone()
            cur = await db.execute("SELECT description FROM products WHERE id = ?", (prod_id,))
            descr = (await cur.fetchone())[0]
            price = 0
            for part in descr.split():
                if part.lower().startswith("price:"):
                    try:
                        price = int(part.split(":")[1])
                    except ValueError:
                        pass
            if price:
                await db.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (price, user_id))
                await db.commit()
        await query.message.reply_text(f"❌ Заказ #{order_id} отклонён.")
        await context.bot.send_message(user_id, f"❌ Ваш заказ #{order_id} отклонён. Сумма {price} ₽ возвращена.")
    elif data.startswith("order_msg_"):
        order_id = int(data.split("_")[2])
        context.user_data["pending_admin_msg_order"] = order_id
        await query.message.reply_text(f"✍️ Введите сообщение для пользователя (заказ #{order_id}).")
    else:
        await query.message.reply_text("❓ Неизвестная команда.")


async def admin_receive_message(update, context):
    admin_id = update.effective_user.id
    if admin_id not in ADMIN_IDS:
        return
    order_id = context.user_data.pop("pending_admin_msg_order", None)
    if not order_id:
        return
    text = update.message.text
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_tg_id FROM orders WHERE id = ?", (order_id,))
        row = await cur.fetchone()
        if not row:
            await update.message.reply_text("❗ Заказ не найден.")
            return
        user_id = row[0]
    await context.bot.send_message(user_id, f"👤 Администратор ответил на ваш заказ #{order_id}:\n\n{text}")
    await update.message.reply_text("✅ Отправлено.")


async def deposit_start(update, context):
    await update.message.reply_text("💳 Введите сумму пополнения (целое число в ₽):")
    return DEPOSIT_AMOUNT


async def deposit_get_amount(update, context):
    try:
        amount = int(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❗ Введите положительное целое число.")
        return DEPOSIT_AMOUNT
    context.user_data["deposit_amount"] = amount
    txt = "ℹ️ Пополнение только через TRON (TRC20).\nАдрес: `TNuD2vFXuWcU6cSpyW8yvQW5sbYuiZDWig`\n\nПосле перевода нажмите «Я отправил» и загрузите скриншот."
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📷 Я отправил", callback_data="deposit_sent")]])
    await update.message.reply_text(txt, reply_markup=kb)
    return DEPOSIT_PROOF


async def deposit_proof(update, context):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("📎 Пришлите скриншот перевода (изображение).")
    return DEPOSIT_PROOF


async def deposit_receive_proof(update, context):
    if not update.message.photo:
        await update.message.reply_text("❗ Пришлите фото.")
        return DEPOSIT_PROOF
    chat_id = update.effective_chat.id
    amount = context.user_data.get("deposit_amount", 0)
    if amount <= 0:
        await update.message.reply_text("❗ Ошибка суммы.")
        return MAIN_MENU
    check_id = str(uuid.uuid4()).replace("-", "").upper()[:10]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO deposits (user_tg_id, amount, check_id, status, created_at) VALUES (?,?,?, 'pending', ?)", (chat_id, amount, check_id, datetime.utcnow().isoformat()))
        await db.commit()
    for admin_id in ADMIN_IDS:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Принять", callback_data=f"deposit_accept_{check_id}"), InlineKeyboardButton("❌ Отклонить", callback_data=f"deposit_reject_{check_id}")]])
        await context.bot.send_message(admin_id, f"💰 Новое пополнение от {chat_id}\nСумма: {amount} ₽\nЧек: {check_id}", reply_markup=kb)
    await update.message.reply_text(f"✅ Ваше пополнение принято в обработку.\nЧек: {check_id}")
    return MAIN_MENU


async def admin_deposit_action(update, context):
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id
    if admin_id not in ADMIN_IDS:
        await query.message.reply_text("❌ Нет прав.")
        return
    data = query.data
    if data.startswith("deposit_accept_"):
        check_id = data.split("_")[2]
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT user_tg_id, amount FROM deposits WHERE check_id = ? AND status = 'pending'", (check_id,))
            row = await cur.fetchone()
            if not row:
                await query.message.reply_text("❗ Чек уже обработан.")
                return
            user_id, amount = row
            await db.execute("UPDATE deposits SET status = 'accepted' WHERE check_id = ?", (check_id,))
            await db.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (amount, user_id))
            await db.commit()
        await query.message.reply_text(f"✅ Пополнение {check_id} принято.")
        await context.bot.send_message(user_id, f"💳 Пополнение на {amount} ₽ принято. Чек: {check_id}")
    elif data.startswith("deposit_reject_"):
        check_id = data.split("_")[2]
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT user_tg_id FROM deposits WHERE check_id = ? AND status = 'pending'", (check_id,))
            row = await cur.fetchone()
            if not row:
                await query.message.reply_text("❗ Чек уже обработан.")
                return
            user_id = row[0]
            await db.execute("UPDATE deposits SET status = 'rejected' WHERE check_id = ?", (check_id,))
            await db.commit()
        await query.message.reply_text(f"❌ Пополнение {check_id} отклонено.")
        await context.bot.send_message(user_id, f"🚫 Пополнение (чек {check_id}) отклонено. Свяжитесь с техподдержкой.")
    else:
        await query.message.reply_text("❓ Неизвестная команда.")


async def seed_products():
    """Добавить стартовые товары из имеющихся фото, если каталог пуст."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM products")
        cnt = (await cur.fetchone())[0]
        if cnt > 0:
            return
        items = [
            ("Конфеты", "Фруктовые жевательные конфеты.\nprice:150", "photo_2026-11-20_14-36-41.jpg"),
            ("Вода", "Минеральная негазированная вода 0.5 л.\nprice:80", "photo_2026-11-20_14-40-41.jpg"),
        ]
        for name, descr, photo in items:
            if os.path.isfile(photo):
                await db.execute("INSERT INTO products (name, description, photo_path) VALUES (?,?,?)", (name, descr, photo))
        await db.commit()


async def about_us(update, context):
    await update.message.reply_photo(photo=load_image(BANNER_DEFAULT), caption="🏢 *О нас*\n\nЗдесь администратор может разместить информацию о компании.")


async def admin_menu(update, context):
    admin_id = update.effective_user.id
    if admin_id not in ADMIN_IDS:
        return
    await update.message.reply_text("⚙️ Административное меню:", reply_markup=make_admin_keyboard())


async def broadcast_start(update, context):
    admin_id = update.effective_user.id
    if admin_id not in ADMIN_IDS:
        return
    await update.message.reply_text("📢 Введите текст для рассылки всем пользователям:")
    return BROADCAST_TEXT


async def broadcast_send(update, context):
    admin_id = update.effective_user.id
    if admin_id not in ADMIN_IDS:
        return
    text = update.message.text
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT tg_id FROM users")
        rows = await cur.fetchall()
    sent = 0
    for (tg_id,) in rows:
        try:
            await context.bot.send_message(tg_id, f"📢 Рассылка:\n\n{text}")
            sent += 1
        except Exception:
            pass
    await update.message.reply_text(f"✅ Рассылка отправлена {sent} пользователям.")
    return MAIN_MENU


async def admin_give_balance_start(update, context):
    admin_id = update.effective_user.id
    if admin_id not in ADMIN_IDS:
        return
    await update.message.reply_text("Введите ID аккаунта пользователя (например #F89JHT):")
    return ADMIN_GIVE_BALANCE_ID


async def admin_give_balance_id(update, context):
    account_id = update.message.text.strip()
    context.user_data["give_balance_account"] = account_id
    await update.message.reply_text("Введите сумму для начисления:")
    return ADMIN_GIVE_BALANCE_AMOUNT


async def admin_give_balance_amount(update, context):
    admin_id = update.effective_user.id
    if admin_id not in ADMIN_IDS:
        return
    try:
        amount = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❗ Введите число.")
        return ADMIN_GIVE_BALANCE_AMOUNT
    account_id = context.user_data.get("give_balance_account")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT tg_id FROM users WHERE account_id = ?", (account_id,))
        row = await cur.fetchone()
        if not row:
            await update.message.reply_text("❗ Пользователь не найден.")
            return MAIN_MENU
        tg_id = row[0]
        await db.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (amount, tg_id))
        await db.commit()
    await update.message.reply_text(f"✅ Начислено {amount} ₽ пользователю {account_id}.")
    await context.bot.send_message(tg_id, f"💰 Вам начислено {amount} ₽ администратором.")
    return MAIN_MENU


async def admin_add_product_start(update, context):
    admin_id = update.effective_user.id
    if admin_id not in ADMIN_IDS:
        return
    await update.message.reply_text("Введите название товара:")
    return ADMIN_ADD_PRODUCT_NAME


async def admin_add_product_name(update, context):
    context.user_data["new_product_name"] = update.message.text.strip()
    await update.message.reply_text("Введите описание (можно указать price:100):")
    return ADMIN_ADD_PRODUCT_DESC


async def admin_add_product_desc(update, context):
    context.user_data["new_product_desc"] = update.message.text.strip()
    await update.message.reply_text("Отправьте фото товара или напишите 'skip':")
    return ADMIN_ADD_PRODUCT_PHOTO


async def admin_add_product_photo(update, context):
    admin_id = update.effective_user.id
    if admin_id not in ADMIN_IDS:
        return
    name = context.user_data.get("new_product_name")
    descr = context.user_data.get("new_product_desc")
    photo_path = None
    if update.message.photo:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        photo_path = f"product_{uuid.uuid4().hex[:8]}.jpg"
        await file.download_to_drive(photo_path)
    elif update.message.text and update.message.text.strip().lower() == "skip":
        photo_path = BANNER_DEFAULT
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO products (name, description, photo_path) VALUES (?,?,?)", (name, descr, photo_path))
        await db.commit()
    await update.message.reply_text("✅ Товар добавлен.")
    return MAIN_MENU


async def admin_delete_product(update, context):
    admin_id = update.effective_user.id
    if admin_id not in ADMIN_IDS:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, name FROM products")
        rows = await cur.fetchall()
    if not rows:
        await update.message.reply_text("🛒 Нет товаров.")
        return
    buttons = [[InlineKeyboardButton(f"🗑️ {name}", callback_data=f"delprod_{pid}")] for pid, name in rows]
    await update.message.reply_text("Выберите товар для удаления:", reply_markup=InlineKeyboardMarkup(buttons))


async def admin_delete_product_cb(update, context):
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id
    if admin_id not in ADMIN_IDS:
        return
    prod_id = int(query.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM products WHERE id = ?", (prod_id,))
        await db.execute("DELETE FROM reviews WHERE product_id = ?", (prod_id,))
        await db.commit()
    await query.message.reply_text("✅ Товар удалён.")


async def admin_review_start(update, context):
    admin_id = update.effective_user.id
    if admin_id not in ADMIN_IDS:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, name FROM products")
        rows = await cur.fetchall()
    buttons = [[InlineKeyboardButton(name, callback_data=f"revprod_{pid}")] for pid, name in rows]
    await update.message.reply_text("Выберите товар для отзыва:", reply_markup=InlineKeyboardMarkup(buttons))


async def admin_review_product(update, context):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[1])
    context.user_data["admin_review_product"] = prod_id
    await query.message.reply_text("Введите текст отзыва:")
    return ADMIN_REVIEW_TEXT


async def admin_review_text(update, context):
    context.user_data["admin_review_text"] = update.message.text.strip()
    await update.message.reply_text("Введите дату отзыва (YYYY-MM-DD HH:MM) или 'now':")
    return ADMIN_REVIEW_DATE


async def admin_review_date(update, context):
    admin_id = update.effective_user.id
    if admin_id not in ADMIN_IDS:
        return
    prod_id = context.user_data.get("admin_review_product")
    text = context.user_data.get("admin_review_text")
    date_str = update.message.text.strip()
    if date_str.lower() == "now":
        created = datetime.utcnow().isoformat()
    else:
        try:
            created = datetime.strptime(date_str, "%Y-%m-%d %H:%M").isoformat()
        except ValueError:
            await update.message.reply_text("❗ Неверный формат. Попробуйте снова.")
            return ADMIN_REVIEW_DATE
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO reviews (product_id, user_tg_id, text, created_at) VALUES (?,?,?,?)", (prod_id, admin_id, text, created))
        await db.commit()
    # удалить старые, оставить 5
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM reviews WHERE product_id = ? ORDER BY created_at DESC", (prod_id,))
        ids = [r[0] for r in await cur.fetchall()]
        if len(ids) > 5:
            to_delete = ids[5:]
            for rid in to_delete:
                await db.execute("DELETE FROM reviews WHERE id = ?", (rid,))
            await db.commit()
    await update.message.reply_text("✅ Отзыв добавлен.")
    return MAIN_MENU


async def admin_settings(update, context):
    admin_id = update.effective_user.id
    if admin_id not in ADMIN_IDS:
        return
    await update.message.reply_text("⚙️ Настройки оплаты: отправьте новый адрес TRON (TRC20) или 'skip'.")


async def text_router(update, context):
    text = update.message.text
    if text == "🛍️ Ассортимент":
        return await show_assortment(update, context)
    if text == "👤 Профиль":
        await profile(update, context)
        return MAIN_MENU
    if text == "ℹ️ О нас":
        await about_us(update, context)
        return MAIN_MENU
    if text == "💬 Тех.поддержка":
        return await support_start(update, context)
    if text == "🔙 Меню":
        await update.message.reply_text("📋 Главное меню", reply_markup=make_main_keyboard())
        return MAIN_MENU
    if text == "📦 Управление заказами":
        await update.message.reply_text("Заказы отображаются в чате при создании.")
        return MAIN_MENU
    if text == "💰 Управление балансом":
        return await admin_give_balance_start(update, context)
    if text == "🛒 Управление товарами":
        admin_id = update.effective_user.id
        if admin_id not in ADMIN_IDS:
            return MAIN_MENU
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Добавить товар", callback_data="admin_add_product")],
            [InlineKeyboardButton("🗑️ Удалить товар", callback_data="admin_del_product")],
            [InlineKeyboardButton("🗣️ Добавить отзыв", callback_data="admin_add_review")],
        ])
        await update.message.reply_text("Управление товарами:", reply_markup=kb)
        return MAIN_MENU
    if text == "📢 Массовая рассылка":
        return await broadcast_start(update, context)
    if text == "⚙️ Настройки оплаты":
        await admin_settings(update, context)
        return MAIN_MENU
    # админское сообщение по заказу
    admin_id = update.effective_user.id
    if admin_id in ADMIN_IDS and context.user_data.get("pending_admin_msg_order"):
        await admin_receive_message(update, context)
        return MAIN_MENU
    return MAIN_MENU


async def admin_panel(update, context):
    admin_id = update.effective_user.id
    if admin_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Доступ только для администраторов.")
        return
    await update.message.reply_text("⚙️ Административное меню:", reply_markup=make_admin_keyboard())


def main():
    asyncio.run(init_db())
    asyncio.run(seed_products())
    app = Application.builder().token(BOT_TOKEN).job_queue(JobQueue()).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CAPTCHA: [CallbackQueryHandler(captcha_handler, pattern="^captcha_")],
            MAIN_MENU: [
                CallbackQueryHandler(show_main_menu, pattern="^show_menu$"),
                CallbackQueryHandler(product_detail, pattern="^product_"),
                CallbackQueryHandler(buy_product_start, pattern="^buy_"),
                CallbackQueryHandler(show_reviews, pattern="^reviews_"),
                CallbackQueryHandler(add_review_prompt, pattern="^addreview_"),
                CallbackQueryHandler(back_to_assort, pattern="^back_to_assort$"),
                CallbackQueryHandler(admin_order_action, pattern="^order_"),
                CallbackQueryHandler(admin_deposit_action, pattern="^deposit_"),
                CallbackQueryHandler(deposit_proof, pattern="^deposit_sent$"),
                CallbackQueryHandler(admin_delete_product_cb, pattern="^delprod_"),
                CallbackQueryHandler(admin_review_product, pattern="^revprod_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_router),
            ],
            SELECT_PRODUCT: [
                CallbackQueryHandler(product_detail, pattern="^product_"),
                CallbackQueryHandler(show_assortment, pattern="^back_to_assort$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_router),
            ],
            PRODUCT_DETAIL: [
                CallbackQueryHandler(buy_product_start, pattern="^buy_"),
                CallbackQueryHandler(show_reviews, pattern="^reviews_"),
                CallbackQueryHandler(add_review_prompt, pattern="^addreview_"),
                CallbackQueryHandler(product_detail, pattern="^product_"),
                CallbackQueryHandler(back_to_assort, pattern="^back_to_assort$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_router),
            ],
            DEPOSIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_get_amount)],
            DEPOSIT_PROOF: [
                CallbackQueryHandler(deposit_proof, pattern="^deposit_sent$"),
                MessageHandler(filters.PHOTO, deposit_receive_proof),
            ],
            SUPPORT_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, support_receive)],
            BROADCAST_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_send)],
            ADMIN_GIVE_BALANCE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_give_balance_id)],
            ADMIN_GIVE_BALANCE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_give_balance_amount)],
            ADMIN_ADD_PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_product_name)],
            ADMIN_ADD_PRODUCT_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_product_desc)],
            ADMIN_ADD_PRODUCT_PHOTO: [MessageHandler(filters.PHOTO | filters.TEXT, admin_add_product_photo)],
            ADMIN_REVIEW_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_review_text)],
            ADMIN_REVIEW_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_review_date)],
        },
        fallbacks=[CommandHandler("start", start)],
        )

    app.add_handler(conv)
    app.add_handler(CommandHandler("admin", admin_panel))
    # callback для кнопок управления товарами (вне диалога)
    app.add_handler(CallbackQueryHandler(admin_add_product_start, pattern="^admin_add_product$"))
    app.add_handler(CallbackQueryHandler(admin_delete_product, pattern="^admin_del_product$"))
    app.add_handler(CallbackQueryHandler(admin_review_start, pattern="^admin_add_review$"))

    # автопроверка закрытых тикетов старше 5 дней
    async def cleanup_tickets(context):
        async with aiosqlite.connect(DB_PATH) as db:
            cutoff = (datetime.utcnow() - timedelta(days=5)).isoformat()
            await db.execute("DELETE FROM tickets WHERE status = 'closed' AND closed_at < ?", (cutoff,))
            await db.commit()

    app.job_queue.run_repeating(cleanup_tickets, interval=3600, first=10)

    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling()


if __name__ == "__main__":
    main()
