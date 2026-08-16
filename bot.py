# -*- coding: utf-8 -*-
"""Телеграм-бот: карточки тренировок, аналитика, дневник еды, напоминания."""

import asyncio
import html
import io
import os
from datetime import datetime

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from PIL import Image

import analytics
import food
from core import (DAILY_PROTEIN_TARGET, TELEGRAM_BOT_TOKEN, ask_gemini_json,
                  get_user_config, load_settings, num, update_user_config)
from google.genai import types as genai_types

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")


class EditWeight(StatesGroup):
    waiting = State()


# ----------------- КАРТОЧКИ УПРАЖНЕНИЙ -----------------
SPLIT_PROGRAM = {
    "day_a": {
        "title": "День А (Грудь + Плечи + Трицепс)",
        "exercises": [
            {
                "name": "Жим штанги лежа",
                "sets": "4x8-10 (RIR 1-2)",
                "media": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Barbell-Bench-Press.gif",
                "setup": "Лопатки сведены и зафиксированы, стопы жестко в полу, умеренный естественный мост.",
                "execution": "Опускание 2-3 сек до низа груди, локти под углом ~75°, мощный жим без отрыва лопаток.",
                "mistake": "Разведение локтей на 90°, отрыв таза.",
            },
            {
                "name": "Армейский жим стоя",
                "sets": "3x10-12 (RIR 1-2)",
                "media": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Overhead-Press.gif",
                "setup": "Хват чуть шире плеч, ягодицы и пресс в жестком замке, нейтральная поясница.",
                "execution": "Траектория грифа строго вертикальная, голова пропускает гриф и возвращается в нейтраль.",
                "mistake": "Прогиб в пояснице, толчок ногами.",
            },
        ],
    },
    "day_b": {
        "title": "День Б (Спина + Бицепс / Брахиалис)",
        "exercises": [
            {
                "name": "Подтягивания",
                "sets": "4xMax (RIR 0-1)",
                "media": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Pull-up.gif",
                "setup": "Полный вис внизу, плечи опущены от ушей, растяжение широчайших.",
                "execution": "Тяга локтями к тазу, грудь тянется к перекладине, контроль негативной фазы.",
                "mistake": "Рывки ногами, неполная амплитуда.",
            },
            {
                "name": "Тяга штанги в наклоне",
                "sets": "4x8-10 (RIR 1-2)",
                "media": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Barbell-Bent-Over-Row.gif",
                "setup": "Наклон корпуса 45-60°, колени мягкие, позвоночник нейтрален.",
                "execution": "Тяга грифа вдоль бедер к низу живота за счет локтей и сведения лопаток.",
                "mistake": "Инерция корпусом, подтягивание веса к груди силой рук.",
            },
            {
                "name": "Молотковые сгибания",
                "sets": "3x12 (RIR 1)",
                "media": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Hammer-Curls.gif",
                "setup": "Нейтральный хват, локти зафиксированы строго по бокам корпуса.",
                "execution": "Подъем без читинга, пауза 1 сек в пиковом сокращении брахиалиса.",
                "mistake": "Заброс веса спиной, вынос локтей вперед.",
            },
        ],
    },
    "day_c": {
        "title": "День C (Ноги + Пресс)",
        "exercises": [
            {
                "name": "Приседания со штангой",
                "sets": "4x10-12 (RIR 1-2)",
                "media": "https://fitnessprogramer.com/wp-content/uploads/2021/02/BARBELL-SQUAT.gif",
                "setup": "Штанга на трапециях, стопы на ширине плеч, носки развернуты на 15-30°, внутрибрюшное давление (Валсальва).",
                "execution": "Колени идут строго по вектору стоп, глубина до параллели, равномерное давление всей стопой.",
                "mistake": "Сведение коленей внутрь, клевок тазом.",
            }
        ],
    },
}


# ----------------- КЛАВИАТУРЫ -----------------
MENU_BUTTONS = ("🏋️ Тренировка дня", "📊 Аналитика", "🍽 Дневник еды", "⚙️ Настройки")


def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏋️ Тренировка дня"), KeyboardButton(text="📊 Аналитика")],
            [KeyboardButton(text="🍽 Дневник еды"), KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True,
    )


DIARY_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📋 Сегодня", callback_data="diary:today"),
     InlineKeyboardButton(text="📅 Неделя", callback_data="diary:week")],
    [InlineKeyboardButton(text="⭐ Избранное", callback_data="diary:fav")],
])


def get_settings_keyboard(chat_id):
    cfg = get_user_config(chat_id)
    m_status = "✅ Вкл" if cfg.get("morning_notify", True) else "❌ Выкл"
    c_status = "✅ Вкл" if cfg.get("casein_notify", True) else "❌ Выкл"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🌅 Утренний сплит (06:00): {m_status}",
                              callback_data="toggle_morning")],
        [InlineKeyboardButton(text=f"🥛 Казеин (21:00): {c_status}",
                              callback_data="toggle_casein")],
        [InlineKeyboardButton(text="🔔 Тест утреннего пуша",
                              callback_data="test_morning_push")],
        [InlineKeyboardButton(text="🔔 Тест казеинового пуша",
                              callback_data="test_casein_push")],
    ])


def meal_keyboard(meal_id, weight_known=True):
    rows = []
    if weight_known:
        rows.append([
            InlineKeyboardButton(text="−50 г", callback_data=f"meal:w-:{meal_id}"),
            InlineKeyboardButton(text="+50 г", callback_data=f"meal:w+:{meal_id}"),
            InlineKeyboardButton(text="✏️ Вес", callback_data=f"meal:wset:{meal_id}"),
        ])
    rows.append([
        InlineKeyboardButton(text="⭐ В избранное", callback_data=f"meal:fav:{meal_id}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"meal:del:{meal_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def favourites_keyboard(favs):
    from core import cut
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🍽 {cut(str(f[1]), 28)}",
                              callback_data=f"fav:log:{f[0]}")]
        for f in favs[:20]
    ])


# ----------------- СЕРВЕР RENDER -----------------
async def handle_ping(request):
    return web.Response(text="Bot is running!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/healthz", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()


# ----------------- НАПОМИНАНИЯ -----------------
async def send_morning_split():
    settings = await asyncio.to_thread(load_settings, True)
    split = SPLIT_PROGRAM[["day_a", "day_b", "day_c"][datetime.now().day % 3]]
    msg = (
        f"🌅 <b>Утренняя сводка</b>\n\n"
        f"🎯 Сегодня по плану: <b>{html.escape(split['title'])}</b>\n"
        f"🥩 Цель по белку: <b>{DAILY_PROTEIN_TARGET} г</b>\n\n"
        f"Нажми «🏋️ Тренировка дня» для карточек с биомеханикой."
    )
    for cid, cfg in settings.items():
        if cfg.get("morning_notify", True):
            try:
                await bot.send_message(chat_id=int(cid), text=msg, parse_mode="HTML")
            except Exception:
                pass


async def send_casein_reminder():
    settings = await asyncio.to_thread(load_settings, True)
    msg = ("🥛 <b>21:00 — вечерний чек-ин</b>\n\n"
           "Время закрыть суточную норму белка перед сном.")
    for cid, cfg in settings.items():
        if cfg.get("casein_notify", True):
            try:
                await bot.send_message(chat_id=int(cid), text=msg, parse_mode="HTML")
            except Exception:
                pass


# ----------------- ОБЩИЕ ХЕНДЛЕРЫ -----------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await asyncio.to_thread(get_user_config, message.chat.id)
    await message.answer(
        "👋 <b>Фитнес-хаб активен.</b>\n\n"
        "🥗 Присылай фото еды, надиктовывай голосом или пиши текстом.\n"
        "🏋️ Карточки, аналитика и дневник — в нижнем меню.\n"
        "🔄 Данные GymUp подтягиваются автоматически.",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML",
    )


@dp.message(F.text == "🏋️ Тренировка дня")
@dp.message(Command("workout"))
async def show_workout(message: types.Message):
    split = SPLIT_PROGRAM[["day_a", "day_b", "day_c"][datetime.now().day % 3]]
    await message.answer(f"📋 <b>ПЛАН: {html.escape(split['title'])}</b>",
                         parse_mode="HTML")

    for ex in split["exercises"]:
        card = (
            f"🏋️ <b>{html.escape(ex['name'])}</b>\n"
            f"📊 Схема: <code>{html.escape(ex['sets'])}</code>\n\n"
            f"📐 <b>Настройка:</b>\n{html.escape(ex['setup'])}\n\n"
            f"🎯 <b>Биомеханика:</b>\n{html.escape(ex['execution'])}\n\n"
            f"⚠️ <b>Ошибка:</b>\n{html.escape(ex['mistake'])}"
        )
        try:
            await message.answer_animation(animation=ex["media"], caption=card,
                                           parse_mode="HTML")
        except Exception:
            await message.answer(card, parse_mode="HTML")


@dp.message(F.text == "📊 Аналитика")
@dp.message(Command("analytics"))
async def show_analytics(message: types.Message):
    status = await message.answer("🔬 Считаю последнюю сессию...")

    rows = await asyncio.to_thread(analytics.read_training_rows)
    if rows is None:
        await status.edit_text("⚠️ Нет доступа к листу «Тренировки».")
        return
    if not rows:
        await status.edit_text("Лист «Тренировки» пуст. Дождись синхронизации GymUp.")
        return

    numbers = await asyncio.to_thread(analytics.build_analytics, rows)
    await status.edit_text(numbers, parse_mode="HTML")

    session_key, facts = await asyncio.to_thread(analytics.collect_facts, rows)
    if not session_key:
        return

    thinking = await message.answer("🧠 Готовлю разбор...")
    text = await asyncio.to_thread(analytics.get_analysis, session_key, facts)
    await thinking.edit_text(text, parse_mode="HTML")


@dp.message(F.text == "⚙️ Настройки")
@dp.message(Command("settings"))
async def show_settings(message: types.Message):
    keyboard = await asyncio.to_thread(get_settings_keyboard, message.chat.id)
    await message.answer("⚙️ <b>Управление уведомлениями</b>", parse_mode="HTML",
                         reply_markup=keyboard)


@dp.callback_query(F.data.startswith("toggle_"))
async def handle_toggle(cb: CallbackQuery):
    cfg = await asyncio.to_thread(get_user_config, cb.message.chat.id)
    key = "morning_notify" if cb.data == "toggle_morning" else "casein_notify"
    await asyncio.to_thread(update_user_config, cb.message.chat.id, key,
                            not cfg.get(key, True))
    keyboard = await asyncio.to_thread(get_settings_keyboard, cb.message.chat.id)
    await cb.message.edit_reply_markup(reply_markup=keyboard)
    await cb.answer("Обновлено")


@dp.callback_query(F.data.startswith("test_"))
async def handle_test_pushes(cb: CallbackQuery):
    if cb.data == "test_morning_push":
        await send_morning_split()
    else:
        await send_casein_reminder()
    await cb.answer("Отправлено")


# ----------------- ДНЕВНИК ЕДЫ -----------------
async def send_meal_card(target, row, edit=False):
    """Показывает карточку приёма пищи: новым сообщением или правкой текущего."""
    rows = await asyncio.to_thread(food.read_meals)
    text = food.format_meal_card(row, food.totals(food.today_meals(rows)))
    markup = meal_keyboard(row[food.M_ID], weight_known=num(row[food.M_WEIGHT]) > 0)
    if edit:
        await target.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)
    return rows


async def log_and_reply(status_msg, data, source):
    """Пишет приём пищи в таблицу и показывает карточку с кнопками."""
    row = food.meal_from_data(data, source)
    await asyncio.to_thread(food.append_meal, row)
    rows = await send_meal_card(status_msg, row, edit=True)

    if await asyncio.to_thread(food.maybe_autofavourite, row[food.M_DISH], rows):
        await status_msg.answer(
            f"⭐ «{html.escape(str(row[food.M_DISH]))}» уехало в избранное — "
            f"ты повторил его {food.FAV_AUTO_AFTER} раза.",
            parse_mode="HTML",
        )


@dp.message(F.text == "🍽 Дневник еды")
@dp.message(Command("diary"))
async def show_diary(message: types.Message):
    await message.answer("🍽 <b>Дневник еды</b>", parse_mode="HTML",
                         reply_markup=DIARY_KEYBOARD)


@dp.callback_query(F.data.startswith("diary:"))
async def handle_diary(cb: CallbackQuery):
    action = cb.data.split(":", 1)[1]

    if action == "today":
        rows = await asyncio.to_thread(food.read_meals)
        await cb.message.answer(food.format_day(food.today_meals(rows)),
                                parse_mode="HTML")
    elif action == "week":
        rows = await asyncio.to_thread(food.read_meals)
        await cb.message.answer(food.format_week(rows), parse_mode="HTML")
    elif action == "fav":
        favs = await asyncio.to_thread(food.read_favourites)
        await cb.message.answer(
            food.format_favourites(favs), parse_mode="HTML",
            reply_markup=favourites_keyboard(favs) if favs else None)
    await cb.answer()


@dp.callback_query(F.data.startswith("fav:log:"))
async def handle_fav_log(cb: CallbackQuery):
    fav_id = cb.data.split(":", 2)[2]
    row = await asyncio.to_thread(food.meal_from_favourite, fav_id)
    if not row:
        await cb.answer("Блюдо не найдено")
        return

    await asyncio.to_thread(food.append_meal, row)
    await asyncio.to_thread(food.bump_favourite, fav_id)
    await send_meal_card(cb.message, row)
    await cb.answer("Записано")


@dp.callback_query(F.data.startswith("meal:"))
async def handle_meal(cb: CallbackQuery, state: FSMContext):
    _, action, meal_id = cb.data.split(":", 2)

    if action == "del":
        ok = await asyncio.to_thread(food.delete_meal, meal_id)
        if ok:
            await cb.message.edit_text("🗑 Запись удалена.")
        await cb.answer("Удалено" if ok else "Не найдено")
        return

    if action == "fav":
        _, row = await asyncio.to_thread(food.find_meal, meal_id)
        if not row:
            await cb.answer("Не найдено")
            return
        await asyncio.to_thread(food.add_favourite, row)
        await cb.answer("⭐ В избранном")
        return

    if action == "wset":
        await state.set_state(EditWeight.waiting)
        await state.update_data(meal_id=meal_id)
        await cb.message.answer("Напиши вес порции в граммах. Отмена — /cancel")
        await cb.answer()
        return

    if action in ("w-", "w+"):
        _, row = await asyncio.to_thread(food.find_meal, meal_id)
        if not row:
            await cb.answer("Не найдено")
            return
        new_weight = num(row[food.M_WEIGHT]) + (50 if action == "w+" else -50)
        if new_weight <= 0:
            await cb.answer("Вес не может быть нулевым")
            return
        updated = await asyncio.to_thread(food.rescale_meal, meal_id, new_weight)
        if not updated:
            await cb.answer("Не удалось пересчитать")
            return
        await send_meal_card(cb.message, updated, edit=True)
        await cb.answer(f"{new_weight:g} г")


@dp.message(Command("cancel"), StateFilter(EditWeight.waiting))
async def cancel_weight(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")


@dp.message(StateFilter(EditWeight.waiting))
async def set_weight(message: types.Message, state: FSMContext):
    weight = num((message.text or "").replace("г", ""))
    if weight <= 0:
        await message.answer("Нужно число больше нуля. Например: 250")
        return

    data = await state.get_data()
    await state.clear()
    updated = await asyncio.to_thread(food.rescale_meal, data.get("meal_id"), weight)
    if not updated:
        await message.answer("Не удалось пересчитать — запись не найдена.")
        return
    await send_meal_card(message, updated)


# ----------------- РАСПОЗНАВАНИЕ ЕДЫ -----------------
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_food_text(message: types.Message):
    if message.text in MENU_BUTTONS:
        return

    status = await message.answer("🔄 Анализирую состав...")
    try:
        prompt = (
            f"Ты спортивный нутрициолог. Разбери продукт или блюдо: '{message.text}'. "
            "Если вес не указан, прими типичную порцию и укажи её в portion_g. "
            + food.FOOD_JSON_SPEC
        )
        data = await asyncio.to_thread(ask_gemini_json, [prompt])
        await log_and_reply(status, data, "текст")
    except Exception:
        await status.edit_text("⚠️ Не удалось разобрать состав.")


@dp.message(F.voice)
async def handle_food_voice(message: types.Message):
    status = await message.answer("🎙️ Распознаю голос и считаю БЖУ...")
    try:
        buffer = io.BytesIO()
        await bot.download(message.voice, destination=buffer)
        buffer.seek(0)

        prompt = ("Ты спортивный нутрициолог. Послушай запись, определи продукты "
                  "и граммовки. " + food.FOOD_JSON_SPEC)
        parts = [genai_types.Part.from_bytes(data=buffer.getvalue(),
                                             mime_type="audio/ogg"), prompt]
        data = await asyncio.to_thread(ask_gemini_json, parts)
        await log_and_reply(status, data, "голос")
    except Exception:
        await status.edit_text("⚠️ Не удалось распознать голос.")


@dp.message(F.photo)
async def handle_food_photo(message: types.Message):
    status = await message.answer("🔍 Распознаю этикетку или блюдо...")
    try:
        buffer = io.BytesIO()
        await bot.download(message.photo[-1], destination=buffer)
        buffer.seek(0)
        img = Image.open(buffer)

        prompt = ("Ты спортивный нутрициолог. Распознай этикетку или блюдо на фото. "
                  "Оцени вес порции по объёму и посуде. "
                  "Если на фото этикетка — считай на всю упаковку. "
                  + food.FOOD_JSON_SPEC)
        data = await asyncio.to_thread(ask_gemini_json, [prompt, img])
        await log_and_reply(status, data, "фото")
    except Exception as e:
        await status.edit_text(f"⚠️ Ошибка обработки: {e}")


# ----------------- ЗАПУСК -----------------
async def main():
    await start_web_server()

    scheduler.add_job(send_morning_split, CronTrigger(hour=6, minute=0))
    scheduler.add_job(send_casein_reminder, CronTrigger(hour=21, minute=0))
    scheduler.start()

    print("Бот готов к работе!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())