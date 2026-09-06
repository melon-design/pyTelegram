"""
Telegram-бот "ГДЗ Агрегатор" на aiogram 3.x
Поиск решений через Google Custom Search API
"""

import asyncio
import logging

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ==================== КОНСТАНТЫ (замените на свои значения) ====================
BOT_TOKEN = "8817651095:AAEVQrMhu33ynL5SFa4RJ6n_G_N34-C3YZA"
GOOGLE_API_KEY = "AIzaSyBF8NNLcQHFbgRTE8-R251kRJloAANn20o"
GOOGLE_CX_ID = "a00cb216b97c648ba"
GOOGLE_SEARCH_URL = "https://www.googleapis.com/customsearch/v1"  # правильный endpoint Google CSE

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()

# ==================== СПИСКИ ДЛЯ КЛАВИАТУР ====================
CLASSES = list(range(5, 12))  # 5..11
SUBJECTS = ["Алгебра/Матем.", "Геометрия", "Русский язык", "Физика", "Химия"]


# ==================== FSM СОСТОЯНИЯ ====================
class GdzSearch(StatesGroup):
    choosing_class = State()
    choosing_subject = State()
    waiting_for_query = State()


# ==================== КЛАВИАТУРЫ ====================
def get_class_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора класса (5-11)."""
    buttons = [
        InlineKeyboardButton(text=f"{cls} класс", callback_data=f"class_{cls}")
        for cls in CLASSES
    ]
    # По 4 кнопки в ряд
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_subject_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора предмета."""
    buttons = [
        InlineKeyboardButton(text=subj, callback_data=f"subject_{subj}")
        for subj in SUBJECTS
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_result_keyboard(link: str) -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой перехода на найденное решение."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👉 Открыть решение на сайте", url=link)]
        ]
    )


# ==================== ХЕНДЛЕР /start ====================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Сбрасывает состояние и показывает выбор класса."""
    await state.clear()
    await state.set_state(GdzSearch.choosing_class)
    await message.answer(
        "👋 Привет! Я твой быстрый агрегатор ГДЗ. Выбери свой класс для поиска решения:",
        reply_markup=get_class_keyboard(),
    )


# ==================== ВЫБОР КЛАССА ====================
@router.callback_query(GdzSearch.choosing_class, F.data.startswith("class_"))
async def process_class_choice(callback: CallbackQuery, state: FSMContext):
    """Сохраняет выбранный класс и предлагает выбрать предмет."""
    chosen_class = callback.data.split("_", 1)[1]
    await state.update_data(school_class=chosen_class)
    await state.set_state(GdzSearch.choosing_subject)

    await callback.message.edit_text(
        f"Выбран {chosen_class} класс. Теперь выбери предмет:",
        reply_markup=get_subject_keyboard(),
    )
    await callback.answer()


# ==================== ВЫБОР ПРЕДМЕТА ====================
@router.callback_query(GdzSearch.choosing_subject, F.data.startswith("subject_"))
async def process_subject_choice(callback: CallbackQuery, state: FSMContext):
    """Сохраняет предмет и запрашивает у пользователя детали задания."""
    subject = callback.data.split("_", 1)[1]
    await state.update_data(subject=subject)
    await state.set_state(GdzSearch.waiting_for_query)

    await callback.message.edit_text(
        "📝 Отлично! Теперь напиши мне автора учебника, параграф и номер задания "
        "одним сообщением. Например: Мордкович параграф 1 номер 1"
    )
    await callback.answer()


# ==================== ФУНКЦИЯ ЗАПРОСА К GOOGLE CUSTOM SEARCH ====================
async def search_gdz(query: str) -> str | None:
    """
    Делает асинхронный запрос к Google Custom Search API.
    Возвращает ссылку на первый результат или None, если ничего не найдено.
    """
    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CX_ID,
        "q": query,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(GOOGLE_SEARCH_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning("Google API вернул статус %s", resp.status)
                    return None
                data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.error("Ошибка запроса к Google API: %s", e)
        return None

    # Защита от пустого/некорректного ответа
    items = data.get("items")
    if not items:
        return None

    first_item = items[0]
    return first_item.get("link")


# ==================== ОБРАБОТКА ТЕКСТА С ЗАДАНИЕМ ====================
@router.message(GdzSearch.waiting_for_query, F.text)
async def process_task_query(message: Message, state: FSMContext):
    """Собирает поисковый запрос, ищет решение и отправляет результат."""
    user_data = await state.get_data()
    school_class = user_data.get("school_class")
    subject = user_data.get("subject")
    user_text = message.text

    # Склеиваем поисковый запрос по шаблону
    search_query = f"ГДЗ {school_class} класс {subject} {user_text}"

    scanning_message = await message.answer("🔍 Сканирую базы ГДЗ, подожди секунду...")

    link = await search_gdz(search_query)

    # Удаляем сообщение о сканировании
    try:
        await scanning_message.delete()
    except Exception:
        pass  # если сообщение уже удалено или недоступно — не критично

    if link:
        await message.answer(
            "✅ Решение успешно найдено! Нажми на кнопку ниже, чтобы открыть его без рекламы:",
            reply_markup=get_result_keyboard(link),
        )
    else:
        await message.answer(
            "❌ По этому запросу ничего не найдено. Попробуй написать проще"
        )

    # Полностью очищаем FSM для нового поиска
    await state.clear()


# ==================== ЗАПУСК БОТА ====================
async def main():
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
