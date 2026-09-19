import asyncio
import csv
import logging
import os
import random
import sys
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite
import aiohttp
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    TelegramObject,
)

# ================== КОНФИГ ==================
BOT_TOKEN = "8669760949:AAG1YOF4lj9Nn9-Tjnw2TAkCs_xdosVJYpQ"
ADMIN_ID = 884518177
CARD_NUMBER = "2202 2083 7620 5804"
CRYPTO_BOT_TOKEN = "622079:AAwvSEzwUa3cpXvqS9RmDFUHUQqrvvUCKye"
DB_PATH = "bot_database.db"
LOG_FILE_PATH = "user_actions.log"

PHOTOS_DIR = "media/photos"
VIDEOS_DIR = "media/videos"
START_DIR = "media/start"

# ================== ЛОГИРОВАНИЕ ==================
class ColoredFormatter(logging.Formatter):
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    RESET = "\033[0m"

    def format(self, record):
        log_fmt = f"{self.CYAN}%(asctime)s{self.RESET} | {self.MAGENTA}%(levelname)s{self.RESET} | %(message)s"
        formatter = logging.Formatter(log_fmt, datefmt="%Y-%m-%d %H:%M:%S")
        return formatter.format(record)


def setup_logger():
    logger = logging.getLogger("bot_logger")
    logger.setLevel(logging.INFO)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ColoredFormatter())
    logger.addHandler(console_handler)
    file_handler = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
    file_formatter = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)
    return logger


action_logger = setup_logger()


class LoggingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user:
            username_str = f"@{user.username}" if user.username else "нет username"
            user_info = f"Имя: {user.full_name} | ID: {user.id} | Ник: {username_str}"
            if isinstance(event, CallbackQuery):
                action_logger.info(f"🖱 [КНОПКА] {user_info} -> [{event.data}]")
            elif isinstance(event, Message):
                msg_text = event.text or "<Медиа>"
                action_logger.info(f"💬 [СООБЩЕНИЕ] {user_info} -> {msg_text}")
        return await handler(event, data)


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
dp.message.middleware(LoggingMiddleware())
dp.callback_query.middleware(LoggingMiddleware())


# ================== БД ==================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                sub_end_date TEXT,
                registered_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                item_name TEXT,
                amount REAL,
                currency TEXT,
                charge_id TEXT,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS contests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                winners_count INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS contest_participants (
                contest_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (contest_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        defaults = {
            "week_rub": "500", "week_stars": "500",
            "month_rub": "1000", "month_stars": "1000",
            "forever_rub": "1500", "forever_stars": "1500",
            "vip_rub": "2000", "vip_stars": "2000",
            "free_try_stars": "10",
        }
        for k, v in defaults.items():
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
        try:
            await db.execute("ALTER TABLE transactions ADD COLUMN charge_id TEXT")
        except Exception:
            pass
        await db.commit()


async def get_setting(key: str, default: str = "0") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else default


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )
        await db.commit()


async def get_tariffs() -> dict:
    return {
        "week": {
            "name": "Неделя", "days": 7,
            "rub": int(await get_setting("week_rub", "500")),
            "stars": int(await get_setting("week_stars", "500")),
        },
        "month": {
            "name": "Месяц", "days": 30,
            "rub": int(await get_setting("month_rub", "1000")),
            "stars": int(await get_setting("month_stars", "1000")),
        },
        "forever": {
            "name": "Навсегда", "days": 0,
            "rub": int(await get_setting("forever_rub", "1500")),
            "stars": int(await get_setting("forever_stars", "1500")),
        },
        "vip": {
            "name": "VIP", "days": 0,
            "rub": int(await get_setting("vip_rub", "2000")),
            "stars": int(await get_setting("vip_stars", "2000")),
        },
    }


async def add_or_update_user(user_id: int, username: str, full_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat()
        await db.execute("""
            INSERT INTO users (user_id, username, full_name, registered_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, full_name = excluded.full_name
        """, (user_id, username, full_name, now))
        await db.commit()


async def set_subscription(user_id: int, days: int = None, is_forever: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        if is_forever:
            end_date_str = "FOREVER"
        else:
            async with db.execute("SELECT sub_end_date FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                current_end = row[0] if row else None
            now = datetime.now()
            if current_end and current_end != "FOREVER":
                try:
                    parsed = datetime.fromisoformat(current_end)
                    if parsed > now:
                        now = parsed
                except ValueError:
                    pass
            end_date_str = (now + timedelta(days=days)).isoformat()
        await db.execute("UPDATE users SET sub_end_date = ? WHERE user_id = ?", (end_date_str, user_id))
        await db.commit()


async def get_user_sub(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT sub_end_date FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row or not row[0]:
                return False, "Не активна"
            sub_end = row[0]
            if sub_end == "FOREVER":
                return True, "Навсегда ♾️"
            end_dt = datetime.fromisoformat(sub_end)
            if end_dt > datetime.now():
                return True, f"до {end_dt.strftime('%d.%m.%Y')}"
            return False, "Истекла ❌"


async def log_transaction(user_id: int, item_name: str, amount: float, currency: str, charge_id: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await db.execute("""
            INSERT INTO transactions (user_id, item_name, amount, currency, charge_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, item_name, amount, currency, charge_id, now))
        await db.commit()


# --- Конкурсы ---
async def create_contest(text: str, winners_count: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat()
        cursor = await db.execute(
            "INSERT INTO contests (text, winners_count, status, created_at) VALUES (?, ?, 'active', ?)",
            (text, winners_count, now)
        )
        await db.commit()
        return cursor.lastrowid


async def get_active_contests():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, text, winners_count, created_at FROM contests WHERE status = 'active' ORDER BY id DESC"
        ) as cursor:
            return await cursor.fetchall()


async def get_contest_participants_count(contest_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM contest_participants WHERE contest_id = ?", (contest_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def is_user_in_contest(contest_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM contest_participants WHERE contest_id = ? AND user_id = ?", (contest_id, user_id)
        ) as cursor:
            return await cursor.fetchone() is not None


async def join_contest(contest_id: int, user_id: int) -> bool:
    if await is_user_in_contest(contest_id, user_id):
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT INTO contest_participants (contest_id, user_id, joined_at) VALUES (?, ?, ?)",
            (contest_id, user_id, now)
        )
        await db.commit()
    return True


async def get_contest_by_id(contest_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, text, winners_count, status FROM contests WHERE id = ?", (contest_id,)) as cursor:
            return await cursor.fetchone()


async def get_contest_participants(contest_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM contest_participants WHERE contest_id = ?", (contest_id,)) as cursor:
            return [r[0] for r in await cursor.fetchall()]


async def finish_contest(contest_id: int, winner_ids: list):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE contests SET status = 'finished' WHERE id = ?", (contest_id,))
        await db.commit()


async def get_all_user_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            return [r[0] for r in await cursor.fetchall()]


def get_random_local_file(folder_path: str):
    if not os.path.exists(folder_path):
        os.makedirs(folder_path, exist_ok=True)
        return None
    files = [os.path.join(folder_path, f) for f in os.listdir(folder_path) if os.path.isfile(os.path.join(folder_path, f))]
    return random.choice(files) if files else None


# ================== CRYPTO BOT ==================
async def create_crypto_invoice(amount_rub: float, description: str, payload: str) -> Optional[dict]:
    """Создаёт инвойс в CryptoBot (fiat RUB)."""
    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}

    params = {
        "currency_type": "fiat",
        "fiat": "RUB",
        "amount": str(amount_rub),
        "description": description[:1024],
        "payload": payload,
        "expires_in": 3600,
        "allow_comments": "false",  # ← было False
        "allow_anonymous": "true",  # ← было True
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return data["result"]
                action_logger.error(f"CryptoBot createInvoice error: {data}")
                return None
    except Exception as e:
        action_logger.error(f"CryptoBot exception: {e}")
        return None

# ================== FSM ==================
class AdminStates(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_days = State()
    waiting_for_refund_user_id = State()
    waiting_contest_text = State()
    waiting_contest_winners = State()
    waiting_finish_contest_id = State()
    waiting_broadcast_text = State()
    waiting_price_rub = State()
    waiting_price_stars = State()
    waiting_anon_reply = State()


class UserStates(StatesGroup):
    waiting_anon_message = State()
    waiting_donate_amount = State()


# ================== КЛАВИАТУРЫ ==================
def get_main_reply_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⚡️ Приват"), KeyboardButton(text="💎 VIP")],
            [KeyboardButton(text="💰 Сборы донатов"), KeyboardButton(text="👤 Моя подписка")],
            [KeyboardButton(text="🏆 Конкурсы"), KeyboardButton(text="🎁 Бесплатный приват")],
            [KeyboardButton(text="✉️ Анонимное сообщение")],
        ],
        resize_keyboard=True,
    )


def get_start_menu_kb():
    """Кнопки как на скриншоте (без лишнего)"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="VIP — 2 000 ₽", callback_data="menu_vip")],
        [InlineKeyboardButton(text="ПРИВАТ", callback_data="menu_private")],
        [InlineKeyboardButton(text="🪙 Сборы донатов", callback_data="menu_donate")],
    ])


async def get_tariffs_kb():
    tariffs = await get_tariffs()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🗓 Неделя — {tariffs['week']['rub']} ₽ / {tariffs['week']['stars']} ⭐️",
            callback_data="sub_week"
        )],
        [InlineKeyboardButton(
            text=f"📅 Месяц — {tariffs['month']['rub']} ₽ / {tariffs['month']['stars']} ⭐️",
            callback_data="sub_month"
        )],
        [InlineKeyboardButton(
            text=f"♾️ Навсегда — {tariffs['forever']['rub']} ₽ / {tariffs['forever']['stars']} ⭐️",
            callback_data="sub_forever"
        )],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="to_main")],
    ])


def get_payment_method_kb(tariff_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Карта (RUB)", callback_data=f"pay_rub_{tariff_key}")],
        [InlineKeyboardButton(text="⭐️ Telegram Stars", callback_data=f"pay_stars_{tariff_key}")],
        [InlineKeyboardButton(text="💎 CryptoBot", callback_data=f"pay_crypto_{tariff_key}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="to_tariffs")],
    ])


def get_check_rub_kb(tariff_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_rub_{tariff_key}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="to_tariffs")],
    ])


def get_try_again_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Попробовать снова (10 ⭐️)", callback_data="free_try_again")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="to_main")],
    ])


def get_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="➕ Выдать подписку", callback_data="admin_give_sub")],
        [InlineKeyboardButton(text="💸 Возврат звёзд", callback_data="admin_refund_stars")],
        [InlineKeyboardButton(text="📥 Выгрузить БД", callback_data="admin_export_users")],
        [InlineKeyboardButton(text="🏆 Создать конкурс", callback_data="admin_create_contest")],
        [InlineKeyboardButton(text="🏁 Завершить конкурс", callback_data="admin_finish_contest")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="💰 Изменить цены", callback_data="admin_change_prices")],
    ])


def get_admin_confirm_rub_kb(user_id: int, tariff_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"adm_confirm_{user_id}_{tariff_key}")],
    ])


async def safe_edit_text(callback: CallbackQuery, text: str, reply_markup=None):
    try:
        await callback.message.edit_text(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    await callback.answer()


# ================== ХЭНДЛЕРЫ ==================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await add_or_update_user(message.from_user.id, message.from_user.username, message.from_user.full_name)

    caption = (
        "Выбери, чтобы узнать подробности каждого тарифа и приобрести! 👇"
    )

    photo_path = get_random_local_file(START_DIR)
    if photo_path:
        await message.answer_photo(
            photo=FSInputFile(photo_path),
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=get_start_menu_kb()
        )
    else:
        await message.answer(
            caption,
            parse_mode=ParseMode.HTML,
            reply_markup=get_start_menu_kb()
        )

    # Дополнительно показываем reply-клавиатуру для удобства
    await message.answer("Меню:", reply_markup=get_main_reply_kb())


# ---------- ГЛАВНОЕ МЕНЮ (inline как на скрине) ----------
@dp.callback_query(F.data == "menu_vip")
async def menu_vip(callback: CallbackQuery):
    tariffs = await get_tariffs()
    vip = tariffs["vip"]
    text = (
        f"💎 <b>VIP доступ</b>\n\n"
        f"Стоимость: <b>{vip['rub']} ₽ / {vip['stars']} ⭐️</b>\n\n"
        f"Выберите способ оплаты:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Карта (RUB)", callback_data="pay_rub_vip")],
        [InlineKeyboardButton(text="⭐️ Telegram Stars", callback_data="pay_stars_vip")],
        [InlineKeyboardButton(text="💎 CryptoBot", callback_data="pay_crypto_vip")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="to_main")],
    ])
    await safe_edit_text(callback, text, kb)


@dp.callback_query(F.data == "menu_private")
async def menu_private(callback: CallbackQuery):
    tariffs = await get_tariffs()
    text = (
        "📊 <b>Цены на Приват</b>\n\n"
        f"<code>┌ Тариф      ┬ Стоимость\n"
        f"├ Неделя     ├ {tariffs['week']['rub']} ₽ / {tariffs['week']['stars']} ⭐️\n"
        f"├ Месяц      ├ {tariffs['month']['rub']} ₽ / {tariffs['month']['stars']} ⭐️\n"
        f"└ Навсегда   └ {tariffs['forever']['rub']} ₽ / {tariffs['forever']['stars']} ⭐️</code>\n\n"
        "<i>Выберите тариф:</i>"
    )
    await safe_edit_text(callback, text, await get_tariffs_kb())


@dp.callback_query(F.data == "menu_donate")
async def menu_donate(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_donate_amount)
    await safe_edit_text(
        callback,
        "💰 <b>Сборы донатов</b>\n\n"
        "Введите сумму доната в рублях (только число):\n\n"
        "Пример: <code>500</code>",
        None
    )


@dp.callback_query(F.data == "to_main")
async def back_to_main(callback: CallbackQuery):
    text = "Выбери, чтобы узнать подробности каждого тарифа и приобрести! 👇"
    await safe_edit_text(callback, text, get_start_menu_kb())


# ---------- REPLY-КНОПКИ (дублируют меню) ----------
@dp.message(F.text == "⚡️ Приват")
async def show_tariffs_reply(message: Message):
    tariffs = await get_tariffs()
    text = (
        "📊 <b>Цены на Приват</b>\n\n"
        f"<code>┌ Тариф      ┬ Стоимость\n"
        f"├ Неделя     ├ {tariffs['week']['rub']} ₽ / {tariffs['week']['stars']} ⭐️\n"
        f"├ Месяц      ├ {tariffs['month']['rub']} ₽ / {tariffs['month']['stars']} ⭐️\n"
        f"└ Навсегда   └ {tariffs['forever']['rub']} ₽ / {tariffs['forever']['stars']} ⭐️</code>\n\n"
        "<i>Выберите тариф:</i>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=await get_tariffs_kb())


@dp.message(F.text == "💎 VIP")
async def show_vip_reply(message: Message):
    tariffs = await get_tariffs()
    vip = tariffs["vip"]
    text = (
        f"💎 <b>VIP доступ</b>\n\n"
        f"Стоимость: <b>{vip['rub']} ₽ / {vip['stars']} ⭐️</b>\n\n"
        f"Выберите способ оплаты:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Карта (RUB)", callback_data="pay_rub_vip")],
        [InlineKeyboardButton(text="⭐️ Telegram Stars", callback_data="pay_stars_vip")],
        [InlineKeyboardButton(text="💎 CryptoBot", callback_data="pay_crypto_vip")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="to_main")],
    ])
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@dp.message(F.text == "💰 Сборы донатов")
async def donate_start_reply(message: Message, state: FSMContext):
    await state.set_state(UserStates.waiting_donate_amount)
    await message.answer(
        "💰 <b>Сборы донатов</b>\n\n"
        "Введите сумму доната в рублях (только число):\n\n"
        "Пример: <code>500</code>",
        parse_mode=ParseMode.HTML
    )


@dp.message(F.text == "👤 Моя подписка")
async def show_profile(message: Message):
    is_active, sub_text = await get_user_sub(message.from_user.id)
    text = (
        f"👤 <b>{message.from_user.full_name}</b>\n\n"
        f"📱 <b>Подписки</b>\n\n"
        f"<code>┌ Тариф      ┬ Действует до\n"
        f"└ Приватный  └ {sub_text}</code>\n\n"
        f"💰 <b>ID:</b> <code>{message.from_user.id}</code>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


# ---------- ДОНАТЫ ----------
@dp.message(UserStates.waiting_donate_amount)
async def donate_amount(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit() or int(message.text) < 1:
        await message.answer("Введите целое положительное число.")
        return

    amount = int(message.text)
    await state.clear()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 На карту", callback_data=f"donate_card_{amount}")],
        [InlineKeyboardButton(text="⭐️ Telegram Stars", callback_data=f"donate_stars_{amount}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="to_main")],
    ])
    await message.answer(
        f"Сумма доната: <b>{amount} ₽</b>\n\nВыберите способ оплаты:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )


@dp.callback_query(F.data.startswith("donate_card_"))
async def donate_card(callback: CallbackQuery):
    amount = callback.data.split("_")[2]
    text = (
        f"💳 <b>Донат на карту</b>\n\n"
        f"Сумма: <code>{amount} RUB</code>\n"
        f"Номер карты: <code>{CARD_NUMBER}</code>\n\n"
        f"После перевода нажмите «Проверить оплату»."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_donate_{amount}")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="to_main")],
    ])
    await safe_edit_text(callback, text, kb)

    user = callback.from_user
    await bot.send_message(
        ADMIN_ID,
        f"💰 <b>Заявка на донат (карта)</b>\n\n"
        f"👤 @{user.username or 'нет'} (<code>{user.id}</code>)\n"
        f"💵 Сумма: <b>{amount} ₽</b>",
        parse_mode=ParseMode.HTML
    )


@dp.callback_query(F.data.startswith("donate_stars_"))
async def donate_stars(callback: CallbackQuery):
    amount = int(callback.data.split("_")[2])
    prices = [LabeledPrice(label=f"Донат {amount} ₽", amount=amount)]
    await callback.message.delete()
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"Донат {amount} ₽",
        description="Поддержка проекта",
        payload=f"donate_{amount}",
        currency="XTR",
        prices=prices,
    )
    await callback.answer()

    user = callback.from_user
    await bot.send_message(
        ADMIN_ID,
        f"⭐️ <b>Заявка на донат (Stars)</b>\n\n"
        f"👤 @{user.username or 'нет'} (<code>{user.id}</code>)\n"
        f"💵 Сумма: <b>{amount} ⭐️</b>",
        parse_mode=ParseMode.HTML
    )


@dp.callback_query(F.data.startswith("check_donate_"))
async def check_donate(callback: CallbackQuery):
    amount = callback.data.split("_")[2]
    user = callback.from_user

    await bot.send_message(
        ADMIN_ID,
        f"🔄 <b>Проверка доната</b>\n\n"
        f"👤 @{user.username or 'нет'} (<code>{user.id}</code>)\n"
        f"💵 Сумма: <b>{amount} ₽</b>\n"
        f"Пользователь нажал «Проверить оплату»",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить донат", callback_data=f"adm_confirm_donate_{user.id}_{amount}")]
        ])
    )

    await safe_edit_text(
        callback,
        "⏳ <b>Заявка отправлена администратору.</b>\nОжидайте подтверждения.",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В меню", callback_data="to_main")]])
    )


@dp.callback_query(F.data.startswith("adm_confirm_donate_"), F.from_user.id == ADMIN_ID)
async def admin_confirm_donate(callback: CallbackQuery):
    parts = callback.data.split("_")
    user_id = int(parts[3])
    amount = parts[4]
    await log_transaction(user_id, "donate", float(amount), "RUB")
    try:
        await bot.send_message(user_id, f"✅ <b>Донат {amount} ₽ подтверждён!</b>\nСпасибо за поддержку!", parse_mode=ParseMode.HTML)
    except Exception:
        pass
    await callback.message.edit_text(callback.message.text + "\n\n✅ <b>ПОДТВЕРЖДЕНО</b>", parse_mode=ParseMode.HTML)
    await callback.answer()


# ---------- БЕСПЛАТНЫЙ ПРИВАТ ----------
@dp.message(F.text == "🎁 Бесплатный приват")
async def free_private_info(message: Message):
    price = await get_setting("free_try_stars", "10")
    text = (
        "🎁 <b>Бесплатный приват</b>\n\n"
        "Попытай удачу и получи доступ к привату + VIP навсегда всего за <b>10 ⭐️</b>!\n\n"
        "🎰 Шанс выигрыша есть всегда!\n"
        "Нажми кнопку ниже, чтобы попробовать."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎲 Попытать удачу (10⭐️)", callback_data="free_try_pay")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="to_main")],
    ])
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@dp.callback_query(F.data.in_({"free_try_pay", "free_try_again"}))
async def free_try_pay(callback: CallbackQuery):
    price = int(await get_setting("free_try_stars", "10"))
    prices = [LabeledPrice(label="Попытка удачи", amount=10)]
    await callback.message.delete()
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Попытка удачи — приват навсегда + VIP",
        description="Шанс получить приват. Удачи!",
        payload="free_try",
        currency="XTR",
        prices=prices ,
    )
    await callback.answer()


# ---------- АНОНИМНОЕ СООБЩЕНИЕ ----------
@dp.message(F.text == "✉️ Анонимное сообщение")
async def anon_start(message: Message, state: FSMContext):
    await state.set_state(UserStates.waiting_anon_message)
    await message.answer(
        "✉️ <b>Анонимное сообщение</b>\n\n"
        "Напиши текст, который хочешь отправить администратору.\n"
        "Твоё имя и username будут видны только админу.\n\n"
        "Отправь сообщение или /cancel для отмены.",
        parse_mode=ParseMode.HTML
    )


@dp.message(UserStates.waiting_anon_message)
async def anon_receive(message: Message, state: FSMContext):
    if message.text and message.text.lower() in ("/cancel", "отмена"):
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_main_reply_kb())
        return

    text = message.text or "<медиа>"
    user = message.from_user
    username = f"@{user.username}" if user.username else "нет username"

    admin_text = (
        f"📩 <b>Анонимное сообщение</b>\n\n"
        f"👤 <b>От:</b> {user.full_name}\n"
        f"🆔 <b>ID:</b> <code>{user.id}</code>\n"
        f"📌 <b>Username:</b> {username}\n\n"
        f"💬 <b>Текст:</b>\n{text}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Ответить", callback_data=f"anon_reply_{user.id}")]
    ])
    await bot.send_message(ADMIN_ID, admin_text, parse_mode=ParseMode.HTML, reply_markup=kb)
    await state.clear()
    await message.answer("✅ Сообщение отправлено.", reply_markup=get_main_reply_kb())


@dp.callback_query(F.data.startswith("anon_reply_"), F.from_user.id == ADMIN_ID)
async def anon_reply_start(callback: CallbackQuery, state: FSMContext):
    target_id = int(callback.data.split("_")[2])
    await state.set_state(AdminStates.waiting_anon_reply)
    await state.update_data(anon_target=target_id)
    await callback.message.answer(f"Введите ответ для пользователя <code>{target_id}</code>:", parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.message(AdminStates.waiting_anon_reply, F.from_user.id == ADMIN_ID)
async def anon_reply_send(message: Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("anon_target")
    await state.clear()
    if not target_id:
        await message.answer("Ошибка: цель не найдена.")
        return
    try:
        await bot.send_message(target_id, f"📨 <b>Ответ от администратора:</b>\n\n{message.text}", parse_mode=ParseMode.HTML)
        await message.answer("✅ Ответ отправлен.")
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить: {e}")


# ---------- КОНКУРСЫ ----------
@dp.message(F.text == "🏆 Конкурсы")
async def show_contests(message: Message):
    contests = await get_active_contests()
    if not contests:
        await message.answer("🏆 <b>Конкурсы</b>\n\nСейчас нет активных конкурсов.\nЗагляните позже!", parse_mode=ParseMode.HTML)
        return

    text = "🏆 <b>Активные конкурсы</b>\n\n"
    buttons = []
    for contest in contests:
        contest_id, contest_text, winners_count, _ = contest
        participants = await get_contest_participants_count(contest_id)
        short_text = contest_text[:80] + "..." if len(contest_text) > 80 else contest_text
        text += f"<b>#{contest_id}</b>\n{short_text}\n👥 Участников: <b>{participants}</b> | 🏆 Победителей: <b>{winners_count}</b>\n\n"
        buttons.append([InlineKeyboardButton(text=f"✅ Участвовать в #{contest_id}", callback_data=f"join_contest_{contest_id}")])
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("join_contest_"))
async def join_contest_handler(callback: CallbackQuery):
    contest_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    contest = await get_contest_by_id(contest_id)
    if not contest or contest[3] != "active":
        await callback.answer("Конкурс уже завершён или не найден.", show_alert=True)
        return
    success = await join_contest(contest_id, user_id)
    if success:
        await callback.answer("✅ Вы успешно участвуете!", show_alert=True)
    else:
        await callback.answer("Вы уже участвуете!", show_alert=True)


@dp.callback_query(F.data == "to_tariffs")
async def back_to_tariffs(callback: CallbackQuery):
    tariffs = await get_tariffs()
    text = (
        "📊 <b>Цены на Приват</b>\n\n"
        f"<code>┌ Тариф      ┬ Стоимость\n"
        f"├ Неделя     ├ {tariffs['week']['rub']} ₽ / {tariffs['week']['stars']} ⭐️\n"
        f"├ Месяц      ├ {tariffs['month']['rub']} ₽ / {tariffs['month']['stars']} ⭐️\n"
        f"└ Навсегда   └ {tariffs['forever']['rub']} ₽ / {tariffs['forever']['stars']} ⭐️</code>\n\n"
        "<i>Выберите тариф:</i>"
    )
    await safe_edit_text(callback, text, await get_tariffs_kb())


@dp.callback_query(F.data.startswith("sub_"))
async def choose_payment_method(callback: CallbackQuery):
    tariff_key = callback.data.split("_")[1]
    tariffs = await get_tariffs()
    tariff = tariffs[tariff_key]
    text = (
        f"💳 <b>Выбран тариф: {tariff['name']}</b>\n\n"
        f"<code>┌ Валюта ┬ К оплате\n"
        f"├ Рубли  ├ {tariff['rub']} ₽\n"
        f"└ Stars  └ {tariff['stars']} ⭐️</code>\n\n"
        f"<i>Выберите метод оплаты:</i>"
    )
    await safe_edit_text(callback, text, get_payment_method_kb(tariff_key))


# ---------- ОПЛАТА ----------
@dp.callback_query(F.data.startswith("pay_rub_"))
async def pay_rub(callback: CallbackQuery):
    tariff_key = callback.data.split("_")[2]
    tariffs = await get_tariffs()
    tariff = tariffs[tariff_key]
    text = (
        f"⚙️ <b>Оплата по реквизитам</b>\n\n"
        f"Сумма: <code>{tariff['rub']} RUB</code>\n"
        f"Карта: <code>{CARD_NUMBER}</code>\n\n"
        f"После перевода нажмите «Проверить оплату»."
    )
    await safe_edit_text(callback, text, get_check_rub_kb(tariff_key))


@dp.callback_query(F.data.startswith("check_rub_"))
async def check_rub_payment(callback: CallbackQuery):
    tariff_key = callback.data.split("_")[2]
    user = callback.from_user
    tariffs = await get_tariffs()
    log_text = (
        f"🚨 <b>НОВАЯ ЗАЯВКА (RUB)</b>\n\n"
        f"👤 @{user.username or 'нет'} (<code>{user.id}</code>)\n"
        f"📦 Тариф: {tariffs[tariff_key]['name']}\n"
        f"💰 Сумма: {tariffs[tariff_key]['rub']} RUB"
    )
    await bot.send_message(ADMIN_ID, log_text, parse_mode=ParseMode.HTML,
                           reply_markup=get_admin_confirm_rub_kb(user.id, tariff_key))
    await safe_edit_text(callback,
                         "⏳ <b>Заявка отправлена. Обычно проверка занимает не больше часа.</b>",
                         InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В меню", callback_data="to_main")]]))


@dp.callback_query(F.data.startswith("pay_crypto_"))
async def pay_crypto(callback: CallbackQuery):
    tariff_key = callback.data.split("_")[2]
    tariffs = await get_tariffs()
    tariff = tariffs[tariff_key]

    invoice = await create_crypto_invoice(
        amount_rub=tariff["rub"],
        description=f"Подписка {tariff['name']}",
        payload=f"sub_{tariff_key}_{callback.from_user.id}"
    )
    if not invoice:
        await callback.answer("Ошибка создания счёта. Попробуйте позже.", show_alert=True)
        return

    pay_url = invoice.get("bot_invoice_url") or invoice.get("pay_url")
    invoice_id = invoice["invoice_id"]

    text = (
        f"💎 <b>CryptoBot</b>\n\n"
        f"Тариф: <b>{tariff['name']}</b>\n"
        f"Сумма: <b>{tariff['rub']} ₽</b>\n\n"
        f"Оплатите по ссылке и нажмите «Проверить»."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Перейти к оплате", url=pay_url)],
        [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_crypto_{invoice_id}_{tariff_key}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="to_tariffs")],
    ])
    await safe_edit_text(callback, text, kb)


@dp.callback_query(F.data.startswith("check_crypto_"))
async def check_crypto_payment(callback: CallbackQuery):
    parts = callback.data.split("_")
    invoice_id = int(parts[2])
    tariff_key = parts[3]
    inv = await get_crypto_invoice(invoice_id)
    if not inv:
        await callback.answer("Не удалось проверить.", show_alert=True)
        return

    if inv.get("status") == "paid":
        tariffs = await get_tariffs()
        tariff = tariffs[tariff_key]
        user_id = callback.from_user.id
        if tariff_key in ("forever", "vip"):
            await set_subscription(user_id, is_forever=True)
        else:
            await set_subscription(user_id, days=tariff["days"])
        await log_transaction(user_id, f"sub_{tariff_key}", tariff["rub"], "CRYPTO", str(invoice_id))
        await callback.message.edit_text(
            "🎉 <b>Оплата подтверждена! Подписка активирована.</b>\n"
            "Ссылка: https://t.me/+qjceWFNFXG82YjUy",
            parse_mode=ParseMode.HTML
        )
        await bot.send_message(ADMIN_ID, f"✅ CryptoBot\nUser: {user_id}\nТариф: {tariff['name']}\n{tariff['rub']} ₽")
    else:
        await callback.answer("Оплата ещё не поступила.", show_alert=True)


@dp.callback_query(F.data.startswith("pay_stars_"))
async def pay_stars_sub(callback: CallbackQuery):
    tariff_key = callback.data.split("_")[2]
    tariffs = await get_tariffs()
    tariff = tariffs[tariff_key]
    prices = [LabeledPrice(label=f"Подписка {tariff['name']}", amount=tariff["stars"])]
    await callback.message.delete()
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"Подписка {tariff['name']}",
        description=f"Оплата доступа — {tariff['name']}",
        payload=f"sub_{tariff_key}",
        currency="XTR",
        prices=prices,
    )
    await callback.answer()


@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payment = message.successful_payment
    user = message.from_user
    payload = payment.invoice_payload
    charge_id = payment.telegram_payment_charge_id

    await log_transaction(user.id, payload, payment.total_amount, "XTR", charge_id)

    await bot.send_message(
        ADMIN_ID,
        f"⭐️ <b>ОПЛАТА STARS</b>\n\n"
        f"👤 @{user.username or 'нет'} (<code>{user.id}</code>)\n"
        f"💰 {payment.total_amount} ⭐️\n"
        f"📦 <code>{payload}</code>\n"
        f"🔑 <code>{charge_id}</code>",
        parse_mode=ParseMode.HTML
    )

    if payload == "free_try":
        await message.answer(
            "😔 <b>К сожалению, вам не повезло...</b>\n\nПопробуйте ещё раз!",
            parse_mode=ParseMode.HTML,
            reply_markup=get_try_again_kb()
        )
        return

    if payload.startswith("donate_"):
        amount = payload.split("_")[1]
        await message.answer(f"✅ <b>Донат {amount} ⭐️ получен!</b>\nСпасибо за поддержку!", parse_mode=ParseMode.HTML)
        return

    if payload.startswith("sub_"):
        tariff_key = payload.split("_")[1]
        tariffs = await get_tariffs()
        tariff = tariffs.get(tariff_key)
        if tariff_key in ("forever", "vip"):
            await set_subscription(user.id, is_forever=True)
        else:
            await set_subscription(user.id, days=tariff["days"])
        await message.answer(
            "🎉 <b>Спасибо за покупку! Подписка активирована.</b>\n"
            "Ссылка: https://t.me/+qjceWFNFXG82YjUy",
            parse_mode=ParseMode.HTML
        )


# ================== АДМИН ==================
@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def cmd_admin(message: Message):
    await message.answer("🔐 <b>Панель администратора:</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_kb())


@dp.callback_query(F.data == "admin_stats", F.from_user.id == ADMIN_ID)
async def admin_stats(callback: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total_users = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE sub_end_date IS NOT NULL") as c:
            active_subs = (await c.fetchone())[0]
        async with db.execute("SELECT SUM(amount) FROM transactions WHERE currency = 'XTR'") as c:
            total_stars = (await c.fetchone())[0] or 0
        async with db.execute("SELECT SUM(amount) FROM transactions WHERE currency = 'RUB'") as c:
            total_rub = (await c.fetchone())[0] or 0
        async with db.execute("SELECT SUM(amount) FROM transactions WHERE currency = 'CRYPTO'") as c:
            total_crypto = (await c.fetchone())[0] or 0

    text = (
        f"📊 <b>Статистика:</b>\n\n"
        f"<code>┌ Пользователи : {total_users}\n"
        f"├ Подписки     : {active_subs}\n"
        f"├ Доход RUB    : {total_rub} ₽\n"
        f"├ Доход Stars  : {total_stars} ⭐️\n"
        f"└ Доход Crypto : {total_crypto} ₽</code>"
    )
    await safe_edit_text(callback, text, get_admin_kb())


@dp.callback_query(F.data.startswith("adm_confirm_"), F.from_user.id == ADMIN_ID)
async def admin_confirm_rub(callback: CallbackQuery):
    parts = callback.data.split("_")
    if parts[2] == "donate":  # уже обработано отдельно
        return
    user_id = int(parts[2])
    tariff_key = parts[3]
    tariffs = await get_tariffs()
    tariff = tariffs[tariff_key]

    if tariff_key in ("forever", "vip"):
        await set_subscription(user_id, is_forever=True)
    else:
        await set_subscription(user_id, days=tariff["days"])

    await log_transaction(user_id, f"sub_{tariff_key}", tariff["rub"], "RUB")

    try:
        await bot.send_message(
            user_id,
            f"✅ <b>Оплата подтверждена!</b>\nТариф <b>{tariff['name']}</b> активирован.\nhttps://t.me/+xQTBhOChj7g2MTc6",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

    await callback.message.edit_text(callback.message.text + "\n\n✅ <b>ПОДТВЕРЖДЕНО</b>", parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.callback_query(F.data == "admin_change_prices", F.from_user.id == ADMIN_ID)
async def admin_change_prices(callback: CallbackQuery, state: FSMContext):
    tariffs = await get_tariffs()
    text = (
        "💰 <b>Текущие цены:</b>\n\n"
        f"Неделя: {tariffs['week']['rub']} ₽ / {tariffs['week']['stars']} ⭐️\n"
        f"Месяц: {tariffs['month']['rub']} ₽ / {tariffs['month']['stars']} ⭐️\n"
        f"Навсегда: {tariffs['forever']['rub']} ₽ / {tariffs['forever']['stars']} ⭐️\n"
        f"VIP: {tariffs['vip']['rub']} ₽ / {tariffs['vip']['stars']} ⭐️\n\n"
        "Выберите тариф:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Неделя", callback_data="price_edit_week")],
        [InlineKeyboardButton(text="Месяц", callback_data="price_edit_month")],
        [InlineKeyboardButton(text="Навсегда", callback_data="price_edit_forever")],
        [InlineKeyboardButton(text="VIP", callback_data="price_edit_vip")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await safe_edit_text(callback, text, kb)


@dp.callback_query(F.data.startswith("price_edit_"), F.from_user.id == ADMIN_ID)
async def price_edit_start(callback: CallbackQuery, state: FSMContext):
    tariff = callback.data.split("_")[2]
    await state.update_data(edit_tariff=tariff)
    await state.set_state(AdminStates.waiting_price_rub)
    await callback.message.answer(f"Введите новую цену в <b>рублях</b> для «{tariff}»:", parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.message(AdminStates.waiting_price_rub, F.from_user.id == ADMIN_ID)
async def price_set_rub(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите целое число.")
        return
    await state.update_data(new_rub=message.text)
    await state.set_state(AdminStates.waiting_price_stars)
    await message.answer("Теперь введите цену в <b>Stars</b>:", parse_mode=ParseMode.HTML)


@dp.message(AdminStates.waiting_price_stars, F.from_user.id == ADMIN_ID)
async def price_set_stars(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите целое число.")
        return
    data = await state.get_data()
    tariff = data["edit_tariff"]
    rub = data["new_rub"]
    stars = message.text
    await set_setting(f"{tariff}_rub", rub)
    await set_setting(f"{tariff}_stars", stars)
    await state.clear()
    await message.answer(f"✅ Цены «{tariff}» обновлены:\nRUB: <b>{rub}</b> ₽\nStars: <b>{stars}</b> ⭐️", parse_mode=ParseMode.HTML)


@dp.callback_query(F.data == "admin_back", F.from_user.id == ADMIN_ID)
async def admin_back(callback: CallbackQuery):
    await safe_edit_text(callback, "🔐 <b>Панель администратора:</b>", get_admin_kb())


@dp.callback_query(F.data == "admin_refund_stars", F.from_user.id == ADMIN_ID)
async def admin_refund_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_refund_user_id)
    await callback.message.answer("Введите ID пользователя для полного возврата всех Stars:")
    await callback.answer()


@dp.message(AdminStates.waiting_for_refund_user_id, F.from_user.id == ADMIN_ID)
async def admin_refund_process(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("ID должен быть числом.")
        return
    target_user_id = int(message.text)
    await state.clear()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT charge_id, amount FROM transactions WHERE user_id = ? AND currency = 'XTR' AND charge_id IS NOT NULL AND charge_id != ''",
            (target_user_id,)
        ) as cursor:
            transactions = await cursor.fetchall()

    if not transactions:
        await message.answer(f"❌ Нет транзакций Stars с Charge ID у <code>{target_user_id}</code>", parse_mode=ParseMode.HTML)
        return

    refunded_count = 0
    total = 0
    for charge_id, amount in transactions:
        try:
            await bot.refund_star_payment(user_id=target_user_id, telegram_payment_charge_id=charge_id)
            refunded_count += 1
            total += amount
        except Exception as e:
            logging.error(f"Refund error {charge_id}: {e}")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE transactions SET charge_id = NULL WHERE user_id = ? AND currency = 'XTR'", (target_user_id,))
        await db.commit()

    await message.answer(
        f"🎉 Возврат выполнен!\n"
        f"Пользователь: <code>{target_user_id}</code>\n"
        f"Транзакций: {refunded_count}/{len(transactions)}\n"
        f"Всего: <b>{total} Stars</b>",
        parse_mode=ParseMode.HTML
    )


@dp.callback_query(F.data == "admin_export_users", F.from_user.id == ADMIN_ID)
async def admin_export_users(callback: CallbackQuery):
    file_path = "users_export.csv"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, username, full_name, sub_end_date, registered_at FROM users") as cursor:
            rows = await cursor.fetchall()
    with open(file_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["User ID", "Username", "Full Name", "Subscription End Date", "Registered At"])
        writer.writerows(rows)
    await callback.message.answer_document(FSInputFile(file_path), caption="📥 Выгрузка пользователей")
    if os.path.exists(file_path):
        os.remove(file_path)
    await callback.answer()


@dp.callback_query(F.data == "admin_give_sub", F.from_user.id == ADMIN_ID)
async def admin_give_sub_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_user_id)
    await callback.message.answer("Введите ID пользователя:")
    await callback.answer()


@dp.message(AdminStates.waiting_for_user_id, F.from_user.id == ADMIN_ID)
async def admin_give_sub_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("ID должен быть числом.")
        return
    await state.update_data(target_user_id=int(message.text))
    await state.set_state(AdminStates.waiting_for_days)
    await message.answer("Количество дней (0 = Навсегда):")


@dp.message(AdminStates.waiting_for_days, F.from_user.id == ADMIN_ID)
async def admin_give_sub_days(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите число.")
        return
    days = int(message.text)
    data = await state.get_data()
    target = data["target_user_id"]
    if days == 0:
        await set_subscription(target, is_forever=True)
        days_str = "Навсегда"
    else:
        await set_subscription(target, days=days)
        days_str = f"{days} дн."
    await state.clear()
    await message.answer(f"✅ Пользователю <code>{target}</code> выдана подписка на <b>{days_str}</b>!", parse_mode=ParseMode.HTML)


@dp.callback_query(F.data == "admin_create_contest", F.from_user.id == ADMIN_ID)
async def admin_create_contest_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_contest_text)
    await callback.message.answer("🏆 Отправьте текст конкурса:")
    await callback.answer()


@dp.message(AdminStates.waiting_contest_text, F.from_user.id == ADMIN_ID)
async def admin_contest_text(message: Message, state: FSMContext):
    await state.update_data(contest_text=message.text)
    await state.set_state(AdminStates.waiting_contest_winners)
    await message.answer("Количество победителей:")


@dp.message(AdminStates.waiting_contest_winners, F.from_user.id == ADMIN_ID)
async def admin_contest_winners(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) < 1:
        await message.answer("Введите число > 0.")
        return
    winners = int(message.text)
    data = await state.get_data()
    cid = await create_contest(data["contest_text"], winners)
    await state.clear()
    await message.answer(f"✅ Конкурс #{cid} создан!\nПобедителей: {winners}", parse_mode=ParseMode.HTML)


@dp.callback_query(F.data == "admin_finish_contest", F.from_user.id == ADMIN_ID)
async def admin_finish_contest_start(callback: CallbackQuery, state: FSMContext):
    contests = await get_active_contests()
    if not contests:
        await callback.answer("Нет активных конкурсов.", show_alert=True)
        return
    text = "🏁 Активные конкурсы:\n\n"
    for c in contests:
        cid, ctext, wcount, _ = c
        parts = await get_contest_participants_count(cid)
        short = ctext[:50] + "..." if len(ctext) > 50 else ctext
        text += f"#{cid} — {short}\n👥 {parts} | 🏆 {wcount}\n\n"
    text += "Введите ID конкурса:"
    await state.set_state(AdminStates.waiting_finish_contest_id)
    await callback.message.answer(text, parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.message(AdminStates.waiting_finish_contest_id, F.from_user.id == ADMIN_ID)
async def admin_finish_contest_process(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("ID — число.")
        return
    contest_id = int(message.text)
    contest = await get_contest_by_id(contest_id)
    if not contest or contest[3] != "active":
        await message.answer("Конкурс не найден / уже завершён.")
        await state.clear()
        return
    _, ctext, wcount, _ = contest
    participants = await get_contest_participants(contest_id)
    if not participants:
        await finish_contest(contest_id, [])
        await state.clear()
        await message.answer(f"Конкурс #{contest_id} завершён. Участников не было.")
        return
    actual = min(wcount, len(participants))
    winners = random.sample(participants, actual)
    await finish_contest(contest_id, winners)
    await state.clear()
    winners_text = "\n".join(f"• <code>{uid}</code>" for uid in winners)
    await message.answer(f"🏁 Конкурс #{contest_id} завершён!\n\n🏆 Победители:\n{winners_text}", parse_mode=ParseMode.HTML)
    for wid in winners:
        try:
            await bot.send_message(wid, f"🎉 Поздравляем! Вы победили в конкурсе #{contest_id}!\n\n{ctext}", parse_mode=ParseMode.HTML)
        except Exception:
            pass


@dp.callback_query(F.data == "admin_broadcast", F.from_user.id == ADMIN_ID)
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_broadcast_text)
    await callback.message.answer("📢 Отправьте текст рассылки (HTML поддерживается):")
    await callback.answer()


@dp.message(AdminStates.waiting_broadcast_text, F.from_user.id == ADMIN_ID)
async def admin_broadcast_process(message: Message, state: FSMContext):
    text = message.text
    await state.clear()
    user_ids = await get_all_user_ids()
    success = failed = 0
    status = await message.answer(f"📢 Рассылка на {len(user_ids)} пользователей...")
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
            success += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"✅ Готово\n✔️ {success}\n❌ {failed}", parse_mode=ParseMode.HTML)


# ================== ЗАПУСК ==================
async def main():
    action_logger.info("🚀 Бот запущен")
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())