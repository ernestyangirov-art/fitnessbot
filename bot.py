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
from PIL import Image

import analytics
import food
import weight
from core import (CALORIE_TARGET_RANGE, DAILY_CALORIE_TARGET,
                  DAILY_PROTEIN_TARGET, PROTEIN_TARGET_RANGE,
                  TELEGRAM_BOT_TOKEN, ask_gemini_json, get_user_config,
                  load_settings, num, update_user_config)
from google.genai import types as genai_types

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Пуши приходят снаружи: GitHub Actions дёргает /cron/<имя> по расписанию.
# Внутренний планировщик на бесплатном Render не работает — сервис засыпает.
CRON_TOKEN = os.getenv("CRON_TOKEN", "")


class EditWeight(StatesGroup):
    waiting = State()


class BodyWeight(StatesGroup):
    waiting = State()


class EditMealField(StatesGroup):
    waiting = State()


class EditTarget(StatesGroup):
    waiting = State()


FIELD_LABELS = food.EDITABLE_FIELDS


# ----------------- ТЕХНИКА ВЫПОЛНЕНИЯ -----------------
# Ключи — точные названия упражнений из листа "Тренировки". Заполнено
# только для того, что уже было описано; для остального карточка
# показывается без блока техники.
EXERCISE_CUES = {
    "Жим штанги лёжа средним хватом": {
        "media": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Barbell-Bench-Press.gif",
        "setup": "Лопатки сведены и зафиксированы, стопы жестко в полу, умеренный естественный мост.",
        "execution": "Опускание 2-3 сек до низа груди, локти под углом ~75°, мощный жим без отрыва лопаток.",
        "mistake": "Разведение локтей на 90°, отрыв таза.",
    },
    "Армейский жим стоя": {
        "media": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Overhead-Press.gif",
        "setup": "Хват чуть шире плеч, ягодицы и пресс в жестком замке, нейтральная поясница.",
        "execution": "Траектория грифа строго вертикальная, голова пропускает гриф и возвращается в нейтраль.",
        "mistake": "Прогиб в пояснице, толчок ногами.",
    },
    "Подтягивания": {
        "media": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Pull-up.gif",
        "setup": "Полный вис внизу, плечи опущены от ушей, растяжение широчайших.",
        "execution": "Тяга локтями к тазу, грудь тянется к перекладине, контроль негативной фазы.",
        "mistake": "Рывки ногами, неполная амплитуда.",
    },
    "Тяга штанги в наклоне": {
        "media": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Barbell-Bent-Over-Row.gif",
        "setup": "Наклон корпуса 45-60°, колени мягкие, позвоночник нейтрален.",
        "execution": "Тяга грифа вдоль бедер к низу живота за счет локтей и сведения лопаток.",
        "mistake": "Инерция корпусом, подтягивание веса к груди силой рук.",
    },
    "Приседания со штангой": {
        "media": "https://fitnessprogramer.com/wp-content/uploads/2021/02/BARBELL-SQUAT.gif",
        "setup": "Штанга на трапециях, стопы на ширине плеч, носки развернуты на 15-30°, внутрибрюшное давление (Валсальва).",
        "execution": "Колени идут строго по вектору стоп, глубина до параллели, равномерное давление всей стопой.",
        "mistake": "Сведение коленей внутрь, клевок тазом.",
    },
}


# ----------------- КЛАВИАТУРЫ -----------------
MENU_BUTTONS = ("🏋️ Тренировка дня", "📊 Аналитика", "🍽 Дневник еды", "⚖️ Вес", "⚙️ Настройки")


def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏋️ Тренировка дня"), KeyboardButton(text="📊 Аналитика")],
            [KeyboardButton(text="🍽 Дневник еды"), KeyboardButton(text="⚖️ Вес")],
            [KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True,
    )


DIARY_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📋 Сегодня", callback_data="diary:today"),
     InlineKeyboardButton(text="📅 Неделя", callback_data="diary:week")],
    [InlineKeyboardButton(text="⭐ Избранное", callback_data="diary:fav")],
])


TARGET_FIELDS = {
    "settings:protein": ("daily_protein_target", PROTEIN_TARGET_RANGE, "белку", "г"),
    "settings:calories": ("daily_calorie_target", CALORIE_TARGET_RANGE, "калориям", "ккал"),
}


def get_settings_keyboard(chat_id):
    cfg = get_user_config(chat_id)
    m_status = "✅ Вкл" if cfg.get("morning_notify", True) else "❌ Выкл"
    c_status = "✅ Вкл" if cfg.get("casein_notify", True) else "❌ Выкл"
    w_status = "✅ Вкл" if cfg.get("weight_notify", True) else "❌ Выкл"
    protein = cfg.get("daily_protein_target", DAILY_PROTEIN_TARGET)
    calories = cfg.get("daily_calorie_target", DAILY_CALORIE_TARGET)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🌅 Утренний сплит (06:00): {m_status}",
                              callback_data="toggle_morning")],
        [InlineKeyboardButton(text=f"🥛 Казеин (21:00): {c_status}",
                              callback_data="toggle_casein")],
        [InlineKeyboardButton(text=f"⚖️ Напоминание о взвешивании: {w_status}",
                              callback_data="toggle_weight")],
        [InlineKeyboardButton(text=f"🥩 Цель по белку: {protein:g} г",
                              callback_data="settings:protein")],
        [InlineKeyboardButton(text=f"🔥 Цель по калориям: {calories:g}",
                              callback_data="settings:calories")],
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
    rows.append([InlineKeyboardButton(text="✏️ Правка БЖУ",
                                      callback_data=f"meal:editmenu:{meal_id}")])
    rows.append([
        InlineKeyboardButton(text="⭐ В избранное", callback_data=f"meal:fav:{meal_id}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"meal:del:{meal_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def field_picker_keyboard(meal_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"meal:setfield:{meal_id}:{key}")]
        for key, label in FIELD_LABELS.items()
    ])


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


async def handle_cron(request):
    """Точка входа для GitHub Actions: /cron/morning, /cron/casein."""
    if not CRON_TOKEN or request.headers.get("X-Cron-Token") != CRON_TOKEN:
        return web.Response(status=403, text="forbidden")

    job = CRON_JOBS.get(request.match_info.get("name", ""))
    if not job:
        return web.Response(status=404, text="unknown job")

    try:
        await job()
    except Exception as e:
        return web.Response(status=500, text=f"failed: {e}")
    return web.Response(text="ok")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/healthz", handle_ping)
    app.router.add_get("/cron/{name}", handle_cron)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()


# ----------------- НАПОМИНАНИЯ -----------------
async def send_morning_split():
    settings = await asyncio.to_thread(load_settings, True)
    rows = await asyncio.to_thread(analytics.read_training_rows)
    splits = await asyncio.to_thread(analytics.predict_splits, rows, 1) if rows else []
    split_name = splits[0] if splits else None

    for cid, cfg in settings.items():
        if cfg.get("morning_notify", True):
            protein = cfg.get("daily_protein_target", DAILY_PROTEIN_TARGET)
            plan_line = (f"🎯 Сегодня по плану: <b>{html.escape(split_name)}</b>\n"
                         if split_name else "")
            msg = (
                f"🌅 <b>Утренняя сводка</b>\n\n"
                f"{plan_line}"
                f"🥩 Цель по белку: <b>{protein:g} г</b>\n\n"
                f"Нажми «🏋️ Тренировка дня» для карточек с биомеханикой."
            )
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


async def send_weight_reminder():
    settings = await asyncio.to_thread(load_settings, True)
    msg = ("⚖️ <b>Утренний замер</b>\n\n"
           "Взвесься натощак и пришли число через «⚖️ Вес» — вес нужен "
           "для корректных расчётов по упражнениям с весом тела.")
    for cid, cfg in settings.items():
        if cfg.get("weight_notify", True):
            try:
                await bot.send_message(chat_id=int(cid), text=msg, parse_mode="HTML")
            except Exception:
                pass


CRON_JOBS = {
    "morning": send_morning_split,
    "casein": send_casein_reminder,
    "weight": send_weight_reminder,
}


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


def workout_keyboard(splits, shown_index):
    from core import cut
    buttons = [
        InlineKeyboardButton(text=cut(name, 24), callback_data=f"workout:pick:{i}")
        for i, name in enumerate(splits) if i != shown_index
    ]
    return InlineKeyboardMarkup(inline_keyboard=[[b] for b in buttons]) if buttons else None


async def send_workout_card(target, rows, splits, shown_index):
    split_name = splits[shown_index]
    exercises = await asyncio.to_thread(analytics.session_exercise_list, rows, split_name)

    await target.answer(f"🏋️ <b>ПЛАН: {html.escape(split_name)}</b>",
                        parse_mode="HTML",
                        reply_markup=workout_keyboard(splits, shown_index))

    if not exercises:
        await target.answer("Для этого сплита ещё нет синхронизированных тренировок.")
        return

    for name, sets in exercises:
        last = ", ".join(sets)
        cue = EXERCISE_CUES.get(name)
        if cue:
            card = (
                f"🏋️ <b>{html.escape(name)}</b>\n"
                f"📊 Последний раз: <code>{html.escape(last)}</code>\n\n"
                f"📐 <b>Настройка:</b>\n{html.escape(cue['setup'])}\n\n"
                f"🎯 <b>Биомеханика:</b>\n{html.escape(cue['execution'])}\n\n"
                f"⚠️ <b>Ошибка:</b>\n{html.escape(cue['mistake'])}"
            )
            try:
                await target.answer_animation(animation=cue["media"], caption=card, parse_mode="HTML")
            except Exception:
                await target.answer(card, parse_mode="HTML")
        else:
            card = (f"🏋️ <b>{html.escape(name)}</b>\n"
                    f"Последний раз: <code>{html.escape(last)}</code>")
            await target.answer(card, parse_mode="HTML")


@dp.message(F.text == "🏋️ Тренировка дня")
@dp.message(Command("workout"))
async def show_workout(message: types.Message):
    rows = await asyncio.to_thread(analytics.read_training_rows)
    if not rows:
        await message.answer("Лист «Тренировки» пуст. Дождись синхронизации GymUp.")
        return
    splits = await asyncio.to_thread(analytics.predict_splits, rows, 4)
    if not splits:
        await message.answer("Не нашёл ни одной тренировки для текущей программы.")
        return
    await send_workout_card(message, rows, splits, 0)


@dp.callback_query(F.data.startswith("workout:pick:"))
async def handle_workout_pick(cb: CallbackQuery):
    idx = int(cb.data.split(":")[2])
    rows = await asyncio.to_thread(analytics.read_training_rows)
    if not rows:
        await cb.answer("Нет данных")
        return
    splits = await asyncio.to_thread(analytics.predict_splits, rows, 4)
    if idx >= len(splits):
        await cb.answer("Уже неактуально")
        return
    await send_workout_card(cb.message, rows, splits, idx)
    await cb.answer()


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


TOGGLE_KEYS = {
    "toggle_morning": "morning_notify",
    "toggle_casein": "casein_notify",
    "toggle_weight": "weight_notify",
}


@dp.callback_query(F.data.startswith("toggle_"))
async def handle_toggle(cb: CallbackQuery):
    cfg = await asyncio.to_thread(get_user_config, cb.message.chat.id)
    key = TOGGLE_KEYS[cb.data]
    await asyncio.to_thread(update_user_config, cb.message.chat.id, key,
                            not cfg.get(key, True))
    keyboard = await asyncio.to_thread(get_settings_keyboard, cb.message.chat.id)
    await cb.message.edit_reply_markup(reply_markup=keyboard)
    await cb.answer("Обновлено")


@dp.callback_query(F.data.in_(list(TARGET_FIELDS)))
async def handle_target_pick(cb: CallbackQuery, state: FSMContext):
    key, rng, label, unit = TARGET_FIELDS[cb.data]
    await state.set_state(EditTarget.waiting)
    await state.update_data(key=key, low=rng[0], high=rng[1], unit=unit)
    await cb.message.answer(
        f"Новая цель по {label}: число {rng[0]}–{rng[1]} {unit}. Отмена — /cancel")
    await cb.answer()


@dp.callback_query(F.data.startswith("test_"))
async def handle_test_pushes(cb: CallbackQuery):
    if cb.data == "test_morning_push":
        await send_morning_split()
    else:
        await send_casein_reminder()
    await cb.answer("Отправлено")


# ----------------- ДНЕВНИК ЕДЫ -----------------
async def user_targets(chat_id):
    cfg = await asyncio.to_thread(get_user_config, chat_id)
    return {"protein": cfg["daily_protein_target"], "calories": cfg["daily_calorie_target"]}


async def send_meal_card(target, row, edit=False):
    """Показывает карточку приёма пищи: новым сообщением или правкой текущего."""
    rows = await asyncio.to_thread(food.read_meals)
    targets = await user_targets(target.chat.id)
    text = food.format_meal_card(row, food.totals(food.today_meals(rows)), targets)
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
        targets = await user_targets(cb.message.chat.id)
        await cb.message.answer(food.format_day(food.today_meals(rows), targets),
                                parse_mode="HTML")
    elif action == "week":
        rows = await asyncio.to_thread(food.read_meals)
        targets = await user_targets(cb.message.chat.id)
        await cb.message.answer(food.format_week(rows, targets), parse_mode="HTML")
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
    parts = cb.data.split(":")
    action, meal_id = parts[1], parts[2]

    if action == "editmenu":
        await cb.message.answer("Что поправить?", reply_markup=field_picker_keyboard(meal_id))
        await cb.answer()
        return

    if action == "setfield":
        field_key = parts[3]
        await state.set_state(EditMealField.waiting)
        await state.update_data(meal_id=meal_id, field_key=field_key)
        hint = "текст" if field_key == "dish" else "число"
        await cb.message.answer(
            f"Новое значение «{FIELD_LABELS[field_key]}» ({hint}). Отмена — /cancel")
        await cb.answer()
        return

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


@dp.message(Command("cancel"), StateFilter(EditMealField.waiting))
async def cancel_meal_field(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")


@dp.message(StateFilter(EditMealField.waiting))
async def set_meal_field(message: types.Message, state: FSMContext):
    data = await state.get_data()
    field_key = data.get("field_key")
    text = (message.text or "").strip()

    if field_key == "dish":
        if not text:
            await message.answer("Название не может быть пустым.")
            return
        value = text
    else:
        value = num(text.replace("г", "").replace("ккал", ""))
        if value < 0:
            await message.answer("Нужно число ≥ 0.")
            return

    await state.clear()
    updated = await asyncio.to_thread(food.edit_meal_field, data.get("meal_id"),
                                      field_key, value)
    if not updated:
        await message.answer("Не удалось сохранить — запись не найдена.")
        return
    await send_meal_card(message, updated)


@dp.message(Command("cancel"), StateFilter(EditTarget.waiting))
async def cancel_target(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")


@dp.message(StateFilter(EditTarget.waiting))
async def set_target(message: types.Message, state: FSMContext):
    data = await state.get_data()
    value = num((message.text or "").replace(",", "."))
    if not (data["low"] <= value <= data["high"]):
        await message.answer(f"Нужно число {data['low']}–{data['high']} {data['unit']}.")
        return

    await state.clear()
    await asyncio.to_thread(update_user_config, message.chat.id, data["key"], round(value))
    keyboard = await asyncio.to_thread(get_settings_keyboard, message.chat.id)
    await message.answer("Обновлено.", parse_mode="HTML", reply_markup=keyboard)


# ----------------- ВЕС ТЕЛА -----------------
@dp.message(F.text == "⚖️ Вес")
@dp.message(Command("weight"))
async def show_weight(message: types.Message, state: FSMContext):
    text = await asyncio.to_thread(weight.format_weight)
    await message.answer(text, parse_mode="HTML")
    await state.set_state(BodyWeight.waiting)
    await message.answer("Пришли текущий вес в кг, например 84.5. Отмена — /cancel")


@dp.message(Command("cancel"), StateFilter(BodyWeight.waiting))
async def cancel_body_weight(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")


@dp.message(StateFilter(BodyWeight.waiting))
async def set_body_weight(message: types.Message, state: FSMContext):
    kg = num((message.text or "").replace("кг", ""))
    if not (weight.MIN_WEIGHT <= kg <= weight.MAX_WEIGHT):
        await message.answer(
            f"Нужно число от {weight.MIN_WEIGHT:g} до {weight.MAX_WEIGHT:g} кг. "
            "Например: 84.5"
        )
        return

    await state.clear()
    ok = await asyncio.to_thread(weight.save_weight, kg)
    if not ok:
        await message.answer("Не удалось сохранить вес.")
        return
    text = await asyncio.to_thread(weight.format_weight)
    await message.answer(text, parse_mode="HTML")


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
    print("Бот готов к работе!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())