"""
Fram — валюта с экосистемой
Telegram-бот на aiogram 3.x, всё в одном файле.

Установка зависимостей:
    pip install aiogram --break-system-packages

Запуск:
    export BOT_TOKEN="ваш_токен_от_BotFather"
    python fram_bot.py

Хранилище: SQLite (fram.db), создаётся автоматически при первом запуске.
"""

import asyncio
import logging
import os
import random
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8836195726:AAGCn1VqbE1q2lcHNM84wRnQC42PeJC8l28")
BOT_USERNAME = "@GO_FRAM_BOT"
DB_PATH = "fram.db"

BONUS_AMOUNT = 500
BONUS_COOLDOWN = timedelta(hours=24)
MIN_TRANSFER = 1

# Названия кнопок
BTN_TRANSFER = "💠 ПЕРЕВЕСТИ"
BTN_POLICY = "📰 ПОЛИТИКА"
BTN_PROFILE = "🪪 КАБИНЕТ"
BTN_BONUS = "🎁 БОНУС"
BTN_CONFIRM = "✅ ПОДТВЕРДИТЬ"
BTN_CANCEL = "❌ ОТМЕНИТЬ"
BTN_PREV = "◀️ НАЗАД"
BTN_NEXT = "ВПЕРЁД ▶️"

# Разделы политики (каждый — отдельная "страница")
POLICY_PAGES = [
    (
        "1️⃣ Общие положения",
        "Валюта Fram является внутренней виртуальной валютой бота "
        "и не имеет реальной денежной стоимости. Она не может быть "
        "обменяна на реальные деньги или иные активы вне экосистемы бота.",
    ),
    (
        "2️⃣ Ответственность",
        "Администрация бота не несёт ответственности за утрату баланса, "
        "ошибочные переводы, действия третьих лиц, а также за любые "
        "последствия использования бота.",
    ),
    (
        "3️⃣ Бонусная программа",
        "Бонус можно активировать только после решения математического "
        "примера. Бонус доступен раз в 24 часа и предоставляется на "
        "усмотрение администрации.",
    ),
    (
        "4️⃣ Изменение правил",
        "Администрация оставляет за собой право изменять правила, "
        "приостанавливать работу бота или обнулять балансы без "
        "предварительного уведомления.",
    ),
    (
        "5️⃣ Нарушения",
        "Запрещены попытки взлома, эксплуатации ошибок и мошенничество — "
        "аккаунты нарушителей могут быть заблокированы.",
    ),
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fram_bot")

# В памяти храним последний показанный пример капчи бонуса,
# чтобы новый пример не повторял предыдущий
_last_bonus_question: dict[int, tuple[int, str, int]] = {}

# --------------------------------------------------------------------------- #
# База данных
# --------------------------------------------------------------------------- #


def db_init() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_id INTEGER NOT NULL,
                to_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Миграция: добавляем недостающие колонки, если база уже существовала
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "last_bonus_at" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_bonus_at TEXT")
        if "clan" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN clan TEXT")
        conn.commit()


def ensure_user(user_id: int, username: str | None) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, balance) VALUES (?, ?, 0)",
            (user_id, username or ""),
        )
        conn.execute(
            "UPDATE users SET username = ? WHERE user_id = ?",
            (username or "", user_id),
        )
        conn.commit()


def get_balance(user_id: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row[0] if row else 0


def get_clan(user_id: int) -> str | None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT clan FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row[0] if row and row[0] else None


def user_exists(user_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row is not None


def get_last_bonus_at(user_id: int) -> datetime | None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT last_bonus_at FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row or not row[0]:
            return None
        return datetime.fromisoformat(row[0])


def bonus_status(user_id: int) -> tuple[bool, timedelta]:
    """Возвращает (доступен ли бонус, оставшееся время ожидания)."""
    last = get_last_bonus_at(user_id)
    if last is None:
        return True, timedelta(0)
    elapsed = datetime.now(timezone.utc) - last
    remaining = BONUS_COOLDOWN - elapsed
    if remaining <= timedelta(0):
        return True, timedelta(0)
    return False, remaining


def claim_bonus(user_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE users SET balance = balance + ?, last_bonus_at = ? WHERE user_id = ?",
            (BONUS_AMOUNT, now, user_id),
        )
        conn.commit()


def transfer_funds(from_id: int, to_id: int, amount: int) -> bool:
    """Атомарный перевод. Возвращает True при успехе."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (from_id,)
        ).fetchone()
        if not row or row[0] < amount:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ?",
            (amount, from_id),
        )
        conn.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (amount, to_id),
        )
        conn.execute(
            "INSERT INTO transfers (from_id, to_id, amount) VALUES (?, ?, ?)",
            (from_id, to_id, amount),
        )
        conn.commit()
        return True


def format_timedelta(td: timedelta) -> str:
    total_seconds = int(td.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours > 0:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


# --------------------------------------------------------------------------- #
# Клавиатуры
# --------------------------------------------------------------------------- #


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TRANSFER)],
            [KeyboardButton(text=BTN_POLICY), KeyboardButton(text=BTN_PROFILE)],
        ],
        resize_keyboard=True,
    )


def profile_kb(bonus_available: bool) -> InlineKeyboardMarkup:
    buttons = []
    if bonus_available:
        buttons.append([InlineKeyboardButton(text=BTN_BONUS, callback_data="bonus")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_transfer_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=BTN_CONFIRM, callback_data="transfer_confirm"),
                InlineKeyboardButton(text=BTN_CANCEL, callback_data="transfer_cancel"),
            ]
        ]
    )


def policy_kb(page: int) -> InlineKeyboardMarkup:
    total = len(POLICY_PAGES)
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text=BTN_PREV, callback_data=f"policy:{page - 1}"))
    row.append(
        InlineKeyboardButton(text=f"{page + 1}/{total}", callback_data="policy_noop")
    )
    if page < total - 1:
        row.append(InlineKeyboardButton(text=BTN_NEXT, callback_data=f"policy:{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def policy_page_text(page: int) -> str:
    title, body = POLICY_PAGES[page]
    return (
        "📰 <b>Политика использования</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"<b>{title}</b>\n\n"
        f"{body}"
    )


# --------------------------------------------------------------------------- #
# FSM состояния
# --------------------------------------------------------------------------- #


class TransferStates(StatesGroup):
    waiting_for_id = State()
    waiting_for_amount = State()
    waiting_for_confirm = State()


class BonusStates(StatesGroup):
    waiting_for_answer = State()


# --------------------------------------------------------------------------- #
# Роутер и хендлеры
# --------------------------------------------------------------------------- #

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "✨ <b>Добро пожаловать в Fram</b> ✨\n"
        "<i>Валюта с экосистемой</i>\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"{BTN_TRANSFER} — переводы Fram\n"
        f"{BTN_PROFILE} — баланс и бонус\n"
        f"{BTN_POLICY} — правила использования\n\n"
        "Выберите действие в меню ниже 👇",
        reply_markup=main_menu_kb(),
    )


@router.message(F.text == BTN_PROFILE)
async def show_profile(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user.id, message.from_user.username)
    balance = get_balance(message.from_user.id)
    clan = get_clan(message.from_user.id)
    bonus_available, remaining = bonus_status(message.from_user.id)

    username = f"@{message.from_user.username}" if message.from_user.username else "не указан"
    clan_text = clan if clan else "не состоите в клане"

    text = (
        "🪪 <b>Ваш кабинет</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"👤 Username: {username}\n"
        f"🛡️ Клан: {clan_text}\n"
        f"💰 Баланс: <b>{balance} Fram</b>\n"
        "━━━━━━━━━━━━━━━━"
    )
    if bonus_available:
        text += "\n\n🎁 Бонус доступен — заберите ниже!"
    else:
        text += f"\n\n⏳ Бонус через {format_timedelta(remaining)}"

    await message.answer(text, reply_markup=profile_kb(bonus_available))


@router.message(F.text == BTN_POLICY)
async def show_policy(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(policy_page_text(0), reply_markup=policy_kb(0))


@router.callback_query(F.data.startswith("policy:"))
async def policy_navigate(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":")[1])
    page = max(0, min(page, len(POLICY_PAGES) - 1))
    await callback.message.edit_text(policy_page_text(page), reply_markup=policy_kb(page))
    await callback.answer()


@router.callback_query(F.data == "policy_noop")
async def policy_noop(callback: CallbackQuery) -> None:
    await callback.answer()


# ---------------------------- Бонус ---------------------------- #


def _generate_bonus_question(user_id: int) -> tuple[int, str, int, int]:
    """Генерирует пример, отличный от предыдущего показанного этому пользователю."""
    previous = _last_bonus_question.get(user_id)
    while True:
        a, b = random.randint(2, 20), random.randint(2, 20)
        op = random.choice(["+", "-", "*"])
        if op == "+":
            answer = a + b
        elif op == "-":
            a, b = max(a, b), min(a, b)
            answer = a - b
        else:
            answer = a * b
        candidate = (a, op, b)
        if candidate != previous:
            _last_bonus_question[user_id] = candidate
            return a, op, b, answer


@router.callback_query(F.data == "bonus")
async def bonus_start(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    available, remaining = bonus_status(user_id)
    if not available:
        await callback.answer(
            f"Бонус через {format_timedelta(remaining)}", show_alert=True
        )
        return

    a, op, b, answer = _generate_bonus_question(user_id)

    await state.update_data(bonus_answer=answer)
    await state.set_state(BonusStates.waiting_for_answer)

    await callback.message.answer(
        f"🎁 <b>Бонус {BONUS_AMOUNT} Fram</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        "Решите пример, чтобы получить награду:\n\n"
        f"🧮 <b>{a} {op} {b} = ?</b>\n\n"
        "Отправьте ответ числом."
    )
    await callback.answer()


@router.message(BonusStates.waiting_for_answer)
async def bonus_check(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    correct_answer = data.get("bonus_answer")

    user_input = (message.text or "").strip()
    try:
        user_answer = int(user_input)
    except ValueError:
        await message.answer("Пожалуйста, введите ответ числом.")
        return

    if user_answer == correct_answer:
        available, _ = bonus_status(message.from_user.id)
        if not available:
            await message.answer(
                "Похоже, бонус уже был получен ранее.", reply_markup=main_menu_kb()
            )
        else:
            claim_bonus(message.from_user.id)
            new_balance = get_balance(message.from_user.id)
            await message.answer(
                "✅ <b>Правильно!</b>\n"
                f"Вам начислено 🎁 <b>{BONUS_AMOUNT} Fram</b>\n\n"
                f"💰 Новый баланс: <b>{new_balance} Fram</b>\n"
                "⏳ Новый бонус через 24 часа.",
                reply_markup=main_menu_kb(),
            )
        await state.clear()
    else:
        await message.answer(
            "❌ Неверный ответ.\n"
            "Попробуйте ещё раз или вернитесь в меню.",
        )


# ---------------------------- Перевод ---------------------------- #


@router.message(F.text == BTN_TRANSFER)
async def transfer_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user.id, message.from_user.username)
    await state.set_state(TransferStates.waiting_for_id)
    await message.answer(
        "💠 <b>Перевод Fram</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        "Введите ID Telegram-аккаунта получателя."
    )


@router.message(TransferStates.waiting_for_id)
async def transfer_get_id(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("ID должен быть числом.\nПопробуйте ещё раз.")
        return

    recipient_id = int(text)

    if recipient_id == message.from_user.id:
        await message.answer("Нельзя перевести валюту самому себе.\nВведите другой ID.")
        return

    if not user_exists(recipient_id):
        await message.answer(
            "Пользователь с таким ID ещё не запускал бота,\n"
            "перевод невозможен."
        )
        return

    await state.update_data(recipient_id=recipient_id)
    await state.set_state(TransferStates.waiting_for_amount)
    await message.answer(f"Укажите сумму перевода (не менее {MIN_TRANSFER} Fram).")


@router.message(TransferStates.waiting_for_amount)
async def transfer_get_amount(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Сумма должна быть положительным числом.\nПопробуйте ещё раз.")
        return

    amount = int(text)
    if amount < MIN_TRANSFER:
        await message.answer(f"Минимальная сумма перевода — {MIN_TRANSFER} Fram.")
        return

    balance = get_balance(message.from_user.id)
    if amount > balance:
        await message.answer(
            f"Недостаточно средств. Ваш баланс: {balance} Fram.\n"
            f"Введите другую сумму."
        )
        return

    data = await state.get_data()
    recipient_id = data["recipient_id"]

    await state.update_data(amount=amount)
    await state.set_state(TransferStates.waiting_for_confirm)

    await message.answer(
        "📤 <b>Подтверждение перевода</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"🆔 Получатель: <code>{recipient_id}</code>\n"
        f"💰 Сумма: <b>{amount} Fram</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        "Подтвердите операцию.",
        reply_markup=confirm_transfer_kb(),
    )


@router.callback_query(TransferStates.waiting_for_confirm, F.data == "transfer_confirm")
async def transfer_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    recipient_id = data.get("recipient_id")
    amount = data.get("amount")

    success = transfer_funds(callback.from_user.id, recipient_id, amount)

    if success:
        new_balance = get_balance(callback.from_user.id)
        await callback.message.edit_text(
            "✅ <b>Перевод выполнен успешно!</b>\n"
            "━━━━━━━━━━━━━━━━\n"
            f"📤 Отправлено: <b>{amount} Fram</b>\n"
            f"🆔 Получатель: <code>{recipient_id}</code>\n"
            f"💰 Ваш новый баланс: <b>{new_balance} Fram</b>"
        )
        try:
            sender_name = (
                f"@{callback.from_user.username}"
                if callback.from_user.username
                else f"ID {callback.from_user.id}"
            )
            await callback.bot.send_message(
                recipient_id,
                "💠 <b>Входящий перевод!</b>\n"
                f"Вам поступило <b>{amount} Fram</b> от {sender_name}.",
            )
        except Exception:
            logger.warning("Не удалось уведомить получателя %s", recipient_id)
    else:
        await callback.message.edit_text(
            "❌ Перевод не выполнен:\n"
            "недостаточно средств на момент подтверждения."
        )

    await state.clear()
    await callback.answer()
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb())


@router.callback_query(TransferStates.waiting_for_confirm, F.data == "transfer_cancel")
async def transfer_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Перевод отменён.")
    await callback.answer()
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb())


# ---------------------------- Фолбэк ---------------------------- #


@router.message()
async def fallback(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Не понимаю эту команду.\n"
        "Пожалуйста, воспользуйтесь меню ниже.",
        reply_markup=main_menu_kb(),
    )


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #


async def main() -> None:
    if BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise RuntimeError(
            "Укажите токен бота в переменной окружения BOT_TOKEN "
            "или напрямую в переменной BOT_TOKEN в коде."
        )

    db_init()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Бот %s запущен", BOT_USERNAME)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
