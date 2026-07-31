import asyncio
import logging
import os
import random
import time

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    TelegramObject,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from typing import Any, Awaitable, Callable

# ============================================================================
# КОНФИГ
# ============================================================================

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "8726918939:AAHEq6HvXx4b8ykPc7pgeJVfcXlJyxSmAnM")
BOT_USERNAME: str = os.getenv("BOT_USERNAME", "your_bot_username")

# ID администраторов, которые видят заявки на вывод и модерируют их.
# Добавляй ID через запятую в .env: ADMIN_IDS=123456789,987654321
ADMIN_IDS: list[int] = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
]

# Каналы/чаты обязательной подписки (ОП).
# Указывай @username канала или chat_id (для приватных).
REQUIRED_CHANNELS: list[str] = [
    c.strip() for c in os.getenv("REQUIRED_CHANNELS", "").split(",") if c.strip()
]

CURRENCY = "₽"
MIN_WITHDRAWAL = 50  # минимальная сумма вывода
REFERRAL_REWARD = 2  # награда пригласившему за выполненного условия реферала

# Минимальный возраст аккаунта реферала (эвристика по user_id), в днях
MIN_ACCOUNT_AGE_DAYS = 90

# Языковые коды, которые эвристически считаем "СНГ"
# (Telegram не даёт страну напрямую — это приближение, а не 100% гарантия)
CIS_LANGUAGE_CODES = {
    "ru", "uk", "be", "kk", "ky", "uz", "tg", "tk",
    "az", "hy", "ka", "mo", "ro",
}

SBP_BANKS = ["Сбер Банк", "Т-Банк", "Альфа Банк"]

DB_PATH = "bot.db"

logging.basicConfig(level=logging.INFO)

# ============================================================================
# БАЗА ДАННЫХ (aiosqlite)
# ============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    balance REAL NOT NULL DEFAULT 0,
    withdrawn_total REAL NOT NULL DEFAULT 0,
    referrer_id INTEGER,
    referrals_count INTEGER NOT NULL DEFAULT 0,
    valid_referrals_count INTEGER NOT NULL DEFAULT 0,
    is_banned INTEGER NOT NULL DEFAULT 0,
    ban_reason TEXT,
    passed_captcha INTEGER NOT NULL DEFAULT 0,
    is_subscribed INTEGER NOT NULL DEFAULT 0,
    is_cis INTEGER NOT NULL DEFAULT 0,
    reward_given INTEGER NOT NULL DEFAULT 0,
    joined_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    method TEXT NOT NULL,        -- 'card' | 'sbp'
    requisite TEXT NOT NULL,     -- номер карты или телефона
    bank TEXT,                   -- банк, если СБП
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | rejected
    reason TEXT,
    created_at INTEGER NOT NULL
);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def get_user(user_id: int) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone()


async def create_user_if_not_exists(
    user_id: int,
    username: str | None,
    referrer_id: int | None,
    is_cis: bool,
) -> bool:
    """Возвращает True, если пользователь был создан только что (новый)."""
    existing = await get_user(user_id)
    if existing:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO users
               (user_id, username, referrer_id, is_cis, joined_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, username, referrer_id, int(is_cis), int(time.time())),
        )
        if referrer_id:
            await db.execute(
                "UPDATE users SET referrals_count = referrals_count + 1 WHERE user_id = ?",
                (referrer_id,),
            )
        await db.commit()
    return True


async def set_username(user_id: int, username: str | None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
        await db.commit()


async def mark_captcha_passed(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET passed_captcha = 1 WHERE user_id = ?", (user_id,))
        await db.commit()


async def mark_subscribed(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_subscribed = 1 WHERE user_id = ?", (user_id,))
        await db.commit()


async def try_reward_referrer(user_id: int, reward: float) -> int | None:
    """
    Если новый пользователь выполнил все условия (капча + подписка) и
    награда ещё не выдавалась — начисляет рефереру награду.
    Возвращает referrer_id, если награда была выдана, иначе None.
    """
    user = await get_user(user_id)
    if not user or user["reward_given"]:
        return None
    if not (user["passed_captcha"] and user["is_subscribed"]):
        return None
    referrer_id = user["referrer_id"]
    if not referrer_id:
        return None

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET reward_given = 1 WHERE user_id = ?", (user_id,)
        )
        await db.execute(
            """UPDATE users SET balance = balance + ?,
               valid_referrals_count = valid_referrals_count + 1
               WHERE user_id = ?""",
            (reward, referrer_id),
        )
        await db.commit()
    return referrer_id


async def ban_user(user_id: int, reason: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_banned = 1, ban_reason = ? WHERE user_id = ?",
            (reason, user_id),
        )
        await db.commit()


async def get_referral_stats(user_id: int) -> dict:
    user = await get_user(user_id)
    if not user:
        return {"total": 0, "valid": 0}
    return {"total": user["referrals_count"], "valid": user["valid_referrals_count"]}


async def create_withdrawal(
    user_id: int, amount: float, method: str, requisite: str, bank: str | None
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO withdrawals (user_id, amount, method, requisite, bank, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, amount, method, requisite, bank, int(time.time())),
        )
        await db.commit()
        return cur.lastrowid


async def get_withdrawal(withdrawal_id: int) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,))
        return await cur.fetchone()


async def set_withdrawal_status(withdrawal_id: int, status: str, reason: str | None = None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE withdrawals SET status = ?, reason = ? WHERE id = ?",
            (status, reason, withdrawal_id),
        )
        await db.commit()


async def apply_withdrawal_effects(user_id: int, amount: float) -> None:
    """Списывает баланс и увеличивает счётчик выведенного при одобрении заявки."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET balance = balance - ?, withdrawn_total = withdrawn_total + ? WHERE user_id = ?",
            (amount, amount, user_id),
        )
        await db.commit()


# ============================================================================
# УТИЛИТЫ: капча, эвристики СНГ / возраста аккаунта
# ============================================================================

def generate_captcha() -> tuple[str, int]:
    """Капча в 2 действия, например: 'Сколько будет 12 + 7?'"""
    a = random.randint(2, 15)
    b = random.randint(2, 15)
    op = random.choice(["+", "-"])
    if op == "-" and a < b:
        a, b = b, a
    question = f"Сколько будет {a} {op} {b}?"
    answer = a + b if op == "+" else a - b
    return question, answer


def is_cis_heuristic(language_code: str | None) -> bool:
    """
    Эвристика "СНГ" по языку интерфейса Telegram.
    Telegram Bot API не отдаёт страну пользователя напрямую — это приближение.
    """
    if not language_code:
        return False
    return language_code.lower() in CIS_LANGUAGE_CODES


# Эвристика возраста аккаунта по user_id.
# Telegram ID растут примерно монотонно со временем регистрации.
# Это приблизительная оценка (публично известные опорные точки id->дата),
# НЕ официальный API — используй только как один из фильтров, не единственный.
_ID_DATE_ANCHORS = [
    (100_000_000, 1_380_000_000),    # ~2013
    (500_000_000, 1_450_000_000),    # ~2016
    (1_000_000_000, 1_540_000_000),  # ~2018
    (1_500_000_000, 1_610_000_000),  # ~2021
    (2_000_000_000, 1_670_000_000),  # ~2022
    (6_000_000_000, 1_720_000_000),  # ~2024
    (7_500_000_000, 1_753_900_000),  # ~2025-2026
]


def estimate_account_age_days(user_id: int) -> int:
    points = _ID_DATE_ANCHORS
    if user_id <= points[0][0]:
        est_ts = points[0][1]
    elif user_id >= points[-1][0]:
        est_ts = points[-1][1]
    else:
        est_ts = points[-1][1]
        for (id1, t1), (id2, t2) in zip(points, points[1:]):
            if id1 <= user_id <= id2:
                ratio = (user_id - id1) / (id2 - id1)
                est_ts = t1 + ratio * (t2 - t1)
                break
    age_seconds = time.time() - est_ts
    return max(0, int(age_seconds // 86400))


def meets_referral_requirements(
    has_avatar: bool,
    has_username: bool,
    is_cis: bool,
    user_id: int,
) -> tuple[bool, list[str]]:
    """Проверяет требования к рефералу. Возвращает (ок?, список нарушений)."""
    violations = []
    if not has_avatar:
        violations.append("нет аватарки")
    if not has_username:
        violations.append("нет @username")
    if estimate_account_age_days(user_id) < MIN_ACCOUNT_AGE_DAYS:
        violations.append("аккаунт младше 3 месяцев")
    if not is_cis:
        violations.append("не из СНГ (по эвристике языка)")
    return (len(violations) == 0, violations)


# ============================================================================
# КЛАВИАТУРЫ
# ============================================================================

def main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="💎 ЗАРАБОТАТЬ", callback_data="menu:earn"))
    b.row(
        InlineKeyboardButton(text="✨ Информация", callback_data="menu:info"),
        InlineKeyboardButton(text="👤 Кабинет", callback_data="menu:cabinet"),
    )
    return b.as_markup()


def back_to_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main"))
    return b.as_markup()


def cabinet_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="💸 Вывод", callback_data="withdraw:start"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main"))
    return b.as_markup()


def withdraw_methods() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="💳 На карту", callback_data="withdraw:method:card"),
        InlineKeyboardButton(text="📱 СБП", callback_data="withdraw:method:sbp"),
    )
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:cabinet"))
    return b.as_markup()


def sbp_banks() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for bank in SBP_BANKS:
        b.row(InlineKeyboardButton(text=f"🏦 {bank}", callback_data=f"withdraw:bank:{bank}"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="withdraw:start"))
    return b.as_markup()


def check_subscription_kb(channels: list[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for ch in channels:
        handle = ch if ch.startswith("http") else f"https://t.me/{ch.lstrip('@')}"
        b.row(InlineKeyboardButton(text="📢 Подписаться", url=handle))
    b.row(InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subscription"))
    return b.as_markup()


def admin_decision_kb(withdrawal_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Принять", callback_data=f"admin:accept:{withdrawal_id}"),
        InlineKeyboardButton(text="❌ Отказать", callback_data=f"admin:reject:{withdrawal_id}"),
    )
    b.row(InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"admin:block:{withdrawal_id}"))
    return b.as_markup()


# ============================================================================
# FSM-СОСТОЯНИЯ
# ============================================================================

class Captcha(StatesGroup):
    waiting_answer = State()


class Withdrawal(StatesGroup):
    waiting_amount = State()
    waiting_card_number = State()
    waiting_phone_number = State()


class AdminReject(StatesGroup):
    waiting_reason = State()


class AdminBlock(StatesGroup):
    waiting_reason = State()


# ============================================================================
# MIDDLEWARE: блокировка забаненных пользователей
# ============================================================================

class BanCheckMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = None
        if isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user

        if user:
            record = await get_user(user.id)
            if record and record["is_banned"]:
                reason = record["ban_reason"] or "нарушение условий реферальной программы"
                text = f"🚫 Вы заблокированы.\nПричина: {reason}"
                if isinstance(event, Message):
                    await event.answer(text)
                elif isinstance(event, CallbackQuery):
                    await event.answer(text, show_alert=True)
                return

        return await handler(event, data)


# ============================================================================
# ПОЛЬЗОВАТЕЛЬСКИЕ ХЭНДЛЕРЫ
# ============================================================================

user_router = Router()


@user_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    args = message.text.split(maxsplit=1)
    referrer_id = None
    if len(args) > 1 and args[1].isdigit():
        potential = int(args[1])
        if potential != message.from_user.id:
            referrer_id = potential

    is_cis = is_cis_heuristic(message.from_user.language_code)
    is_new = await create_user_if_not_exists(
        user_id=message.from_user.id,
        username=message.from_user.username,
        referrer_id=referrer_id,
        is_cis=is_cis,
    )
    await set_username(message.from_user.id, message.from_user.username)

    user = await get_user(message.from_user.id)

    if is_new:
        question, answer = generate_captcha()
        await state.update_data(captcha_answer=answer)
        await state.set_state(Captcha.waiting_answer)
        await message.answer(
            "👋 <b>Добро пожаловать!</b>\n\n"
            "Прежде чем продолжить, пройди короткую проверку 🔐\n\n"
            f"❓ {question}"
        )
        return

    if not user["passed_captcha"]:
        question, answer = generate_captcha()
        await state.update_data(captcha_answer=answer)
        await state.set_state(Captcha.waiting_answer)
        await message.answer(f"🔐 Реши пример, чтобы продолжить:\n\n❓ {question}")
        return

    if not user["is_subscribed"]:
        await send_subscription_gate(message)
        return

    await show_main_menu(message)


@user_router.message(Captcha.waiting_answer)
async def captcha_check(message: Message, state: FSMContext):
    data = await state.get_data()
    correct = data.get("captcha_answer")

    if not message.text or not message.text.strip().lstrip("-").isdigit():
        await message.answer("✏️ Введи ответ числом.")
        return

    if int(message.text.strip()) == correct:
        await mark_captcha_passed(message.from_user.id)
        await state.clear()
        await message.answer("✅ <b>Проверка пройдена!</b>")
        await send_subscription_gate(message)
    else:
        question, answer = generate_captcha()
        await state.update_data(captcha_answer=answer)
        await message.answer(f"❌ Неверно, попробуй ещё раз.\n\n❓ {question}")


async def send_subscription_gate(message: Message):
    if not REQUIRED_CHANNELS:
        await mark_subscribed(message.from_user.id)
        await finalize_referral(message.bot, message.from_user.id)
        await show_main_menu(message)
        return

    await message.answer(
        "📢 <b>Обязательная подписка</b>\n\n"
        "Чтобы пользоваться ботом, подпишись на наши каналы, "
        "а затем нажми «Я подписался» 👇",
        reply_markup=check_subscription_kb(REQUIRED_CHANNELS),
    )


@user_router.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery, bot: Bot):
    not_subscribed = []
    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=callback.from_user.id)
            if member.status in ("left", "kicked"):
                not_subscribed.append(channel)
        except Exception:
            not_subscribed.append(channel)

    if not_subscribed:
        await callback.answer("❗ Ты подписался ещё не на все каналы.", show_alert=True)
        return

    await mark_subscribed(callback.from_user.id)
    await finalize_referral(bot, callback.from_user.id)
    await callback.message.edit_text("✅ <b>Подписка подтверждена!</b>")
    await show_main_menu(callback.message)
    await callback.answer()


async def finalize_referral(bot: Bot, user_id: int) -> None:
    """После капчи+подписки — начисляет награду пригласившему, если реферал валиден."""
    user = await get_user(user_id)
    if not user or not user["referrer_id"]:
        return

    try:
        chat = await bot.get_chat(user_id)
        has_avatar = bool(chat.photo)
    except Exception:
        has_avatar = False

    ok, violations = meets_referral_requirements(
        has_avatar=has_avatar,
        has_username=bool(user["username"]),
        is_cis=bool(user["is_cis"]),
        user_id=user_id,
    )

    if ok:
        referrer_id = await try_reward_referrer(user_id, REFERRAL_REWARD)
        if referrer_id:
            try:
                await bot.send_message(
                    referrer_id,
                    f"🎉 Твой реферал выполнил все условия!\n"
                    f"Начислено: <b>+{REFERRAL_REWARD}{CURRENCY}</b> ✨",
                )
            except Exception:
                pass
    else:
        reason = ", ".join(violations)
        await ban_user(
            user["referrer_id"],
            f"приглашённый реферал не соответствует требованиям: {reason}",
        )
        try:
            await bot.send_message(
                user["referrer_id"],
                "🚫 <b>Вы заблокированы.</b>\n"
                f"Причина: приглашённый реферал не подошёл по условиям ({reason}).",
            )
        except Exception:
            pass


async def show_main_menu(message: Message):
    await message.answer(
        "✨ <b>Главное меню</b>\n\nВыбери, что тебя интересует 👇",
        reply_markup=main_menu(),
    )


@user_router.callback_query(F.data == "menu:main")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "✨ <b>Главное меню</b>\n\nВыбери, что тебя интересует 👇",
        reply_markup=main_menu(),
    )
    await callback.answer()


@user_router.callback_query(F.data == "menu:earn")
async def cb_earn(callback: CallbackQuery):
    stats = await get_referral_stats(callback.from_user.id)
    link = f"https://t.me/{BOT_USERNAME}?start={callback.from_user.id}"

    text = (
        "💎 <b>Заработать</b>\n\n"
        "Приглашай друзей и получай награду за каждого активного реферала!\n\n"
        f"👥 Всего приглашено: <b>{stats['total']}</b>\n"
        f"✅ Засчитано (выполнили условия): <b>{stats['valid']}</b>\n"
        f"💰 Награда за реферала: <b>+{REFERRAL_REWARD}{CURRENCY}</b>\n\n"
        f"🔗 Твоя реферальная ссылка:\n<code>{link}</code>"
    )
    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()


@user_router.callback_query(F.data == "menu:info")
async def cb_info(callback: CallbackQuery):
    text = (
        "✨ <b>Информация о боте</b>\n\n"
        "💎 <b>Заработать</b> — приглашай друзей по своей ссылке и получай "
        f"награду <b>+{REFERRAL_REWARD}{CURRENCY}</b> за каждого, кто пройдёт "
        "проверку и подпишется на каналы.\n\n"
        "👤 <b>Кабинет</b> — твой профиль, баланс и вывод средств.\n\n"
        f"💸 <b>Вывод</b> — минимальная сумма вывода: <b>{MIN_WITHDRAWAL}{CURRENCY}</b>. "
        "Доступны способы: карта и СБП (Сбер, Т-Банк, Альфа Банк).\n\n"
        "📋 <b>Требования к рефералам:</b>\n"
        "• аватарка на аккаунте\n"
        "• указан @username\n"
        "• аккаунт старше 3 месяцев\n"
        "• аккаунт из СНГ\n\n"
        "⚠️ Приглашение реферала, который не соответствует требованиям, "
        "приведёт к блокировке."
    )
    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()


@user_router.callback_query(F.data == "menu:cabinet")
async def cb_cabinet(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    username = f"@{user['username']}" if user["username"] else "—"
    text = (
        "👤 <b>Кабинет</b>\n\n"
        f"🆔 ID: <code>{user['user_id']}</code>\n"
        f"👤 Username: {username}\n"
        f"💰 Баланс: <b>{user['balance']:.2f}{CURRENCY}</b>\n"
        f"📤 Выведено всего: <b>{user['withdrawn_total']:.2f}{CURRENCY}</b>"
    )
    await callback.message.edit_text(text, reply_markup=cabinet_menu())
    await callback.answer()


@user_router.callback_query(F.data == "withdraw:start")
async def cb_withdraw_start(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    if user["balance"] < MIN_WITHDRAWAL:
        await callback.answer(
            f"❗ Минимальная сумма вывода — {MIN_WITHDRAWAL}{CURRENCY}. "
            f"Твой баланс: {user['balance']:.2f}{CURRENCY}",
            show_alert=True,
        )
        return

    await state.set_state(Withdrawal.waiting_amount)
    await callback.message.edit_text(
        f"💸 <b>Вывод средств</b>\n\n"
        f"Баланс: <b>{user['balance']:.2f}{CURRENCY}</b>\n"
        f"Минимальная сумма: <b>{MIN_WITHDRAWAL}{CURRENCY}</b>\n\n"
        "Введи сумму, которую хочешь вывести:",
        reply_markup=back_to_main(),
    )
    await callback.answer()


@user_router.message(Withdrawal.waiting_amount)
async def withdraw_amount(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)

    if not message.text or not message.text.replace(".", "", 1).isdigit():
        await message.answer("✏️ Введи сумму числом.")
        return

    amount = float(message.text)
    if amount < MIN_WITHDRAWAL:
        await message.answer(f"❗ Минимальная сумма вывода — {MIN_WITHDRAWAL}{CURRENCY}.")
        return
    if amount > user["balance"]:
        await message.answer(f"❗ На балансе только {user['balance']:.2f}{CURRENCY}.")
        return

    await state.update_data(amount=amount)
    await message.answer(
        "✅ Сумма принята.\n\nВыбери способ вывода:",
        reply_markup=withdraw_methods(),
    )


@user_router.callback_query(F.data.startswith("withdraw:method:"))
async def withdraw_method(callback: CallbackQuery, state: FSMContext):
    method = callback.data.split(":")[-1]
    await state.update_data(method=method)

    if method == "card":
        await state.set_state(Withdrawal.waiting_card_number)
        await callback.message.edit_text(
            "💳 Введи номер карты, на которую нужно вывести средства:",
            reply_markup=back_to_main(),
        )
    else:
        await state.set_state(Withdrawal.waiting_phone_number)
        await callback.message.edit_text(
            "📱 Введи номер телефона, привязанный к СБП:",
            reply_markup=back_to_main(),
        )
    await callback.answer()


@user_router.message(Withdrawal.waiting_card_number)
async def withdraw_card_number(message: Message, state: FSMContext):
    card = message.text.strip() if message.text else ""
    digits = card.replace(" ", "")
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        await message.answer("❗ Введи корректный номер карты.")
        return

    await state.update_data(requisite=card)
    await finish_withdrawal_request(message, state, bank=None)


@user_router.message(Withdrawal.waiting_phone_number)
async def withdraw_phone_number(message: Message, state: FSMContext):
    phone = message.text.strip() if message.text else ""
    digits = phone.replace("+", "").replace(" ", "").replace("-", "")
    if not digits.isdigit() or not (10 <= len(digits) <= 15):
        await message.answer("❗ Введи корректный номер телефона.")
        return

    await state.update_data(requisite=phone)
    await message.answer("🏦 Выбери банк для перевода по СБП:", reply_markup=sbp_banks())


@user_router.callback_query(F.data.startswith("withdraw:bank:"))
async def withdraw_bank(callback: CallbackQuery, state: FSMContext):
    bank = callback.data.split(":", 2)[-1]
    await state.update_data(bank=bank)
    await finish_withdrawal_request(callback.message, state, bank=bank, from_callback=callback)


async def finish_withdrawal_request(
    message: Message, state: FSMContext, bank: str | None, from_callback: CallbackQuery | None = None
):
    data = await state.get_data()
    user_id = from_callback.from_user.id if from_callback else message.from_user.id
    user = await get_user(user_id)

    withdrawal_id = await create_withdrawal(
        user_id=user_id,
        amount=data["amount"],
        method=data["method"],
        requisite=data["requisite"],
        bank=bank,
    )
    await state.clear()

    text = (
        "✅ <b>Заявка на вывод отправлена на модерацию!</b>\n\n"
        f"💰 Сумма: <b>{data['amount']:.2f}{CURRENCY}</b>\n"
        f"💳 Способ: {'Карта' if data['method'] == 'card' else 'СБП' + (f' ({bank})' if bank else '')}\n\n"
        "Ожидай решения администратора ⏳"
    )

    if from_callback:
        await message.edit_text(text, reply_markup=back_to_main())
    else:
        await message.answer(text, reply_markup=back_to_main())

    await notify_admins_new_withdrawal(message.bot, withdrawal_id, user)


async def notify_admins_new_withdrawal(bot: Bot, withdrawal_id: int, user):
    w = await get_withdrawal(withdrawal_id)
    stats = await get_referral_stats(user["user_id"])
    username = f"@{user['username']}" if user["username"] else "—"

    requisite_line = (
        f"💳 Карта: <code>{w['requisite']}</code>"
        if w["method"] == "card"
        else f"📱 СБП ({w['bank']}): <code>{w['requisite']}</code>"
    )

    text = (
        "🆕 <b>Новая заявка на вывод</b>\n\n"
        f"🆔 ID: <code>{user['user_id']}</code>\n"
        f"👤 Username: {username}\n"
        f"💰 Баланс: {user['balance']:.2f}{CURRENCY}\n"
        f"📤 Выведено всего: {user['withdrawn_total']:.2f}{CURRENCY}\n\n"
        f"💵 Сумма заявки: <b>{w['amount']:.2f}{CURRENCY}</b>\n"
        f"{requisite_line}\n\n"
        f"👥 Рефералов всего: {stats['total']} | Засчитано: {stats['valid']}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=admin_decision_kb(withdrawal_id))
        except Exception:
            pass


# ============================================================================
# АДМИН-ХЭНДЛЕРЫ
# ============================================================================

admin_router = Router()
admin_router.message.filter(lambda m: m.from_user.id in ADMIN_IDS)
admin_router.callback_query.filter(lambda c: c.from_user.id in ADMIN_IDS)


@admin_router.callback_query(F.data.startswith("admin:accept:"))
async def admin_accept(callback: CallbackQuery, bot: Bot):
    withdrawal_id = int(callback.data.split(":")[-1])
    w = await get_withdrawal(withdrawal_id)

    if not w or w["status"] != "pending":
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return

    await apply_withdrawal_effects(w["user_id"], w["amount"])
    await set_withdrawal_status(withdrawal_id, "accepted")

    await callback.message.edit_text(
        callback.message.text + "\n\n✅ <b>ПРИНЯТО</b>",
        parse_mode="HTML",
    )
    await callback.answer("Заявка принята ✅")

    try:
        await bot.send_message(
            w["user_id"],
            f"✅ Твоя заявка на вывод <b>{w['amount']:.2f}{CURRENCY}</b> одобрена!",
        )
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("admin:reject:"))
async def admin_reject_start(callback: CallbackQuery, state: FSMContext):
    withdrawal_id = int(callback.data.split(":")[-1])
    w = await get_withdrawal(withdrawal_id)

    if not w or w["status"] != "pending":
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return

    await state.update_data(
        withdrawal_id=withdrawal_id,
        origin_chat_id=callback.message.chat.id,
        origin_message_id=callback.message.message_id,
    )
    await state.set_state(AdminReject.waiting_reason)
    await callback.message.answer("✏️ Напиши причину отказа:")
    await callback.answer()


@admin_router.message(StateFilter(AdminReject.waiting_reason))
async def admin_reject_reason(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    withdrawal_id = data["withdrawal_id"]
    reason = message.text

    w = await get_withdrawal(withdrawal_id)
    await set_withdrawal_status(withdrawal_id, "rejected", reason)
    await state.clear()

    await message.answer(f"❌ Заявка #{withdrawal_id} отклонена.")

    try:
        await bot.edit_message_text(
            chat_id=data["origin_chat_id"],
            message_id=data["origin_message_id"],
            text="🆕 Заявка обработана.\n\n❌ <b>ОТКАЗАНО</b>\nПричина: " + reason,
            parse_mode="HTML",
        )
    except Exception:
        pass

    try:
        await bot.send_message(
            w["user_id"],
            f"❌ Твоя заявка на вывод <b>{w['amount']:.2f}{CURRENCY}</b> отклонена.\n"
            f"Причина: {reason}",
        )
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("admin:block:"))
async def admin_block_start(callback: CallbackQuery, state: FSMContext):
    withdrawal_id = int(callback.data.split(":")[-1])
    w = await get_withdrawal(withdrawal_id)
    if not w:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    await state.update_data(
        withdrawal_id=withdrawal_id,
        target_user_id=w["user_id"],
        origin_chat_id=callback.message.chat.id,
        origin_message_id=callback.message.message_id,
    )
    await state.set_state(AdminBlock.waiting_reason)
    await callback.message.answer("✏️ Напиши причину блокировки пользователя:")
    await callback.answer()


@admin_router.message(StateFilter(AdminBlock.waiting_reason))
async def admin_block_reason(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    reason = message.text
    target_user_id = data["target_user_id"]
    withdrawal_id = data["withdrawal_id"]

    await ban_user(target_user_id, reason)
    await set_withdrawal_status(withdrawal_id, "rejected", f"Блокировка: {reason}")
    await state.clear()

    await message.answer(f"🚫 Пользователь {target_user_id} заблокирован.")

    try:
        await bot.edit_message_text(
            chat_id=data["origin_chat_id"],
            message_id=data["origin_message_id"],
            text="🆕 Заявка обработана.\n\n🚫 <b>ПОЛЬЗОВАТЕЛЬ ЗАБЛОКИРОВАН</b>\nПричина: " + reason,
            parse_mode="HTML",
        )
    except Exception:
        pass

    try:
        await bot.send_message(target_user_id, f"🚫 Вы заблокированы.\nПричина: {reason}")
    except Exception:
        pass


# ============================================================================
# ТОЧКА ВХОДА
# ============================================================================

async def main():
    await init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.middleware(BanCheckMiddleware())
    dp.callback_query.middleware(BanCheckMiddleware())

    # admin_router первым, чтобы фильтр "только админы" отрабатывал раньше
    dp.include_router(admin_router)
    dp.include_router(user_router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
