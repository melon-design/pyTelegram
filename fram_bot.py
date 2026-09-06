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
MIN_TRANSFER = 1

POLICY_TEXT = (
    "📰 <b>Политика использования</b>\n"
    "━━━━━━━━━━━━━━━━\n"
    "Используя данного бота, вы соглашаетесь со следующими условиями:\n\n"
    "1️⃣ Валюта Fram является внутренней виртуальной валютой бота и не имеет "
    "реальной денежной стоимости.\n\n"
    "2️⃣ Администрация бота не несёт ответственности за утрату баланса, "
    "ошибочные переводы, действия третьих лиц, а также за любые "
    "последствия использования бота.\n\n"
    "3️⃣ Бонус «🎁 Бонус» можно активировать только после решения "
    "математического примера и предоставляется на усмотрение администрации.\n\n"
    "4️⃣ Администрация оставляет за собой право изменять правила, "
    "приостанавливать работу бота или обнулять балансы без предварительного "
    "уведомления.\n\n"
    "5️⃣ Запрещены попытки взлома, эксплуатации ошибок и мошенничество — "
    "аккаунты нарушителей могут быть заблокированы.\n"
    "━━━━━━━━━━━━━━━━"
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fram_bot")

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
                balance INTEGER NOT NULL DEFAULT 0,
                bonus_claimed INTEGER NOT NULL DEFAULT 0
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


def user_exists(user_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row is not None


def has_claimed_bonus(user_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT bonus_claimed FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return bool(row[0]) if row else False


def claim_bonus(user_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE users SET balance = balance + ?, bonus_claimed = 1 WHERE user_id = ?",
            (BONUS_AMOUNT, user_id),
        )
        conn.commit()


def transfer_funds(from_id: int, to_id: int, amount: int) -> bool:
    """Атомарный перевод. Возвращает True при успехе."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute("BEGIN IMMEDIATE")
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


# --------------------------------------------------------------------------- #
# Клавиатуры
# --------------------------------------------------------------------------- #


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💠 Перевести")],
            [KeyboardButton(text="📰 Политика"), KeyboardButton(text="💼 Профиль")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие в меню ⬇️",
    )


def profile_kb(bonus_available: bool) -> InlineKeyboardMarkup:
    buttons = []
    if bonus_available:
        buttons.append(
            [InlineKeyboardButton(text="🎁 Бонус", callback_data="bonus")]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_transfer_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data="transfer_confirm"),
                InlineKeyboardButton(text="❌ Отменить", callback_data="transfer_cancel"),
            ]
        ]
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
        "💠 <b>Перевести</b> — отправить Fram другому пользователю\n"
        "💼 <b>Профиль</b> — ваш баланс и бонус\n"
        "📰 <b>Политика</b> — правила использования\n\n"
        "Выберите действие в меню ниже 👇",
        reply_markup=main_menu_kb(),
    )


@router.message(F.text == "💼 Профиль")
async def show_profile(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user.id, message.from_user.username)
    balance = get_balance(message.from_user.id)
    bonus_available = not has_claimed_bonus(message.from_user.id)

    username = f"@{message.from_user.username}" if message.from_user.username else "не указан"

    text = (
        "💼 <b>Ваш профиль</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"👤 Username: {username}\n"
        f"💰 Баланс: <b>{balance} Fram</b>\n"
        "━━━━━━━━━━━━━━━━"
    )
    if not bonus_available:
        text += "\n\n✅ Бонус уже был получен ранее."
    else:
        text += "\n\n🎁 Вам доступен бонус — заберите его ниже!"

    await message.answer(text, reply_markup=profile_kb(bonus_available))


@router.message(F.text == "📰 Политика")
async def show_policy(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(POLICY_TEXT)


# ---------------------------- Бонус ---------------------------- #


@router.callback_query(F.data == "bonus")
async def bonus_start(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    if has_claimed_bonus(user_id):
        await callback.answer("Вы уже получили бонус ранее.", show_alert=True)
        return

    a, b = random.randint(2, 20), random.randint(2, 20)
    op = random.choice(["+", "-", "*"])
    if op == "+":
        answer = a + b
    elif op == "-":
        a, b = max(a, b), min(a, b)  # чтобы не было отрицательного результата
        answer = a - b
    else:
        answer = a * b

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
        if has_claimed_bonus(message.from_user.id):
            await message.answer(
                "Похоже, бонус уже был получен ранее.", reply_markup=main_menu_kb()
            )
        else:
            claim_bonus(message.from_user.id)
            new_balance = get_balance(message.from_user.id)
            await message.answer(
                "✅ <b>Правильно!</b>\n"
                f"Вам начислено 🎁 <b>{BONUS_AMOUNT} Fram</b>\n\n"
                f"💰 Новый баланс: <b>{new_balance} Fram</b>",
                reply_markup=main_menu_kb(),
            )
        await state.clear()
    else:
        await message.answer(
            "❌ Неверный ответ. Попробуйте ещё раз или вернитесь в меню.",
        )


# ---------------------------- Перевод ---------------------------- #


@router.message(F.text == "💠 Перевести")
async def transfer_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user.id, message.from_user.username)
    await state.set_state(TransferStates.waiting_for_id)
    await message.answer(
        "💠 <b>Перевод Fram</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        "Введите ID Telegram-аккаунта получателя.\n\n"
        "ℹ️ Узнать свой ID можно, например, через бота @userinfobot."
    )


@router.message(TransferStates.waiting_for_id)
async def transfer_get_id(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("ID должен быть числом. Попробуйте ещё раз.")
        return

    recipient_id = int(text)

    if recipient_id == message.from_user.id:
        await message.answer("Нельзя перевести валюту самому себе. Введите другой ID.")
        return

    if not user_exists(recipient_id):
        await message.answer(
            "Пользователь с таким ID ещё не запускал бота, перевод невозможен.\n"
            "Попросите его нажать /start в этом боте, затем повторите попытку."
        )
        return

    await state.update_data(recipient_id=recipient_id)
    await state.set_state(TransferStates.waiting_for_amount)
    await message.answer(f"Укажите сумму перевода (не менее {MIN_TRANSFER} Fram).")


@router.message(TransferStates.waiting_for_amount)
async def transfer_get_amount(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Сумма должна быть положительным числом. Попробуйте ещё раз.")
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
            "❌ Перевод не выполнен: недостаточно средств на момент подтверждения."
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
        "Не понимаю эту команду. Пожалуйста, воспользуйтесь меню ниже.",
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
