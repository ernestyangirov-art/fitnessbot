import asyncio
import io
import json
import os
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
import google.generativeai as genai
import gspread
from google.oauth2 import service_account
from PIL import Image

load_dotenv()

# ----------------- КОНФИГУРАЦИЯ -----------------
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN", "8867533906:AAHfN9ZtDWlurbjk4mhWj-bF2ZMlREPF0JA")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1TcDkwfY1R0wrvQQ6PdETSOIzVOvCmjJs4YWvkS-Gkb0")

DAILY_PROTEIN_TARGET = 150   # г
DAILY_CALORIE_TARGET = 2300  # ккал
DATA_FILE = "subscribers.json"

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")

# ----------------- ФЕЙКОВЫЙ ВЕБ-СЕРВЕР ДЛЯ RENDER -----------------
async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/healthz", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# ----------------- БАЗА ПОДПИСЧИКОВ -----------------
def load_subscribers():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_subscriber(chat_id):
    subscribers = load_subscribers()
    if chat_id not in subscribers:
        subscribers.add(chat_id)
        with open(DATA_FILE, "w") as f:
            json.dump(list(subscribers), f)

# ----------------- БАЗА УПРАЖНЕНИЙ -----------------
SPLIT_PROGRAM = {
    "day_a": {
        "title": "День А (Грудь + Плечи + Трицепс)",
        "exercises": [
            {"name": "Жим лежа", "sets": "4x8-10", "cue": "Сведение лопаток, стабильный мост, угол локтей ~75°.", "gif": "https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjEx/giphy.gif"},
            {"name": "Армейский жим стоя", "sets": "3x10-12", "cue": "Пресс и ягодицы зажаты, гриф опускается до верха груди.", "gif": "https://media.giphy.com/media/3o7TKMt1VVNkHV2PaE/giphy.gif"}
        ]
    },
    "day_b": {
        "title": "День Б (Спина + Бицепс / Брахиалис)",
        "exercises": [
            {"name": "Подтягивания", "sets": "4xMax", "cue": "Тяга локтями к тазу, полный контроль внизу.", "gif": "https://media.giphy.com/media/uV9l8p6JvU1zZqK3uE/giphy.gif"},
            {"name": "Тяга штанги в наклоне", "sets": "4x8-10", "cue": "Корпус 45-60°, тяга к поясу/паху.", "gif": "https://media.giphy.com/media/3o7TKMt1VVNkHV2PaE/giphy.gif"},
            {"name": "Молотковые сгибания", "sets": "3x12", "cue": "Нейтральный хват, фиксация локтей у корпуса.", "gif": "https://media.giphy.com/media/l41lM8A5pBAH7UWWY/giphy.gif"}
        ]
    },
    "day_c": {
        "title": "День C (Ноги + Пресс)",
        "exercises": [
            {"name": "Приседания", "sets": "4x10-12", "cue": "Колени по направлению носков, спина нейтральна.", "gif": "https://media.giphy.com/media/3o7TKMt1VVNkHV2PaE/giphy.gif"}
        ]
    }
}

# ----------------- GOOGLE ТАБЛИЦА -----------------
def get_sheets_client():
    creds_json = os.environ.get("GCP_CREDENTIALS")
    if creds_json:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(creds_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
    else:
        creds = service_account.Credentials.from_service_account_file(
            "credentials.json",
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
    return gspread.authorize(creds)

def sync_log_food_and_get_daily_total(meal_name: str, protein: float, calories: float):
    client = get_sheets_client()
    sheet = client.open_by_key(SPREADSHEET_ID)
    
    try:
        ws = sheet.worksheet("Питание")
    except Exception:
        ws = sheet.add_worksheet(title="Питание", rows=1000, cols=5)
        ws.append_row(["Дата и время", "Блюдо/Продукты", "Белки (г)", "Калории (ккал)"])

    now = datetime.now()
    ws.append_row([now.strftime("%Y-%m-%d %H:%M:%S"), meal_name, protein, calories])

    today_str = now.strftime("%Y-%m-%d")
    all_records = ws.get_all_values()
    
    total_protein, total_calories = 0.0, 0.0
    for row in all_records[1:]:
        if len(row) >= 4 and row[0].startswith(today_str):
            try:
                total_protein += float(str(row[2]).replace(",", "."))
                total_calories += float(str(row[3]).replace(",", "."))
            except ValueError:
                continue

    return total_protein, total_calories

def format_feedback(dish_name: str, protein: float, calories: float, total_p: float, total_c: float) -> str:
    left_p = max(0.0, DAILY_PROTEIN_TARGET - total_p)
    return (
        f"✅ **Записано в Google Таблицу:**\n"
        f"🍽️ *{dish_name}* (+{protein:.1f}г белка, +{calories:.0f} ккал)\n\n"
        f"🥩 **Белок за сегодня:** {total_p:.0f} / {DAILY_PROTEIN_TARGET} г\n"
        f"🔥 **Калории:** {total_c:.0f} / {DAILY_CALORIE_TARGET} ккал\n"
        f"⏳ **До сна нужно добрать:** {left_p:.0f} г белка"
    )

# ----------------- НАПОМИНАНИЯ -----------------
async def send_morning_split():
    subscribers = load_subscribers()
    day_key = ["day_a", "day_b", "day_c"][datetime.now().day % 3]
    split_info = SPLIT_PROGRAM[day_key]
    
    for user_id in subscribers:
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"🌅 **Утренняя сводка & Сплит дня**\n\n"
                    f"🎯 Сегодня по плану: **{split_info['title']}**\n"
                    f"🥩 Цель по белку: **{DAILY_PROTEIN_TARGET} г** | 🔥 Калории: **{DAILY_CALORIE_TARGET} ккал**\n\n"
                    "Для просмотра техники упражнений нажмите /workout"
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass

async def send_casein_reminder():
    subscribers = load_subscribers()
    for user_id in subscribers:
        try:
            await bot.send_message(
                chat_id=user_id,
                text="🥛 **21:00 — Время казеина!**\n\nНе забудьте порцию казеина перед сном для ночного восстановления мышц.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

# ----------------- ХЕНДЛЕРЫ -----------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    save_subscriber(message.chat.id)
    await message.answer(
        "👋 **Добро пожаловать в персональный фитнес-хаб!**\n\n"
        "🥗 **Питание:**\n"
        "• Отправьте **фото блюда**\n"
        "• Напишите текстом: `3 яйца и порция протеина`\n"
        "• Отправьте **голосовое сообщение**\n\n"
        "🏋️ **Тренировки:**\n"
        "• `/workout` — план тренировки с техникой и GIF\n"
        "• `/settings` — проверка уведомлений",
        parse_mode="Markdown"
    )

@dp.message(Command("workout"))
async def cmd_workout(message: types.Message):
    day_key = ["day_a", "day_b", "day_c"][datetime.now().day % 3]
    split = SPLIT_PROGRAM[day_key]
    
    await message.answer(f"📋 **План тренировки: {split['title']}**", parse_mode="Markdown")
    for ex in split["exercises"]:
        caption = (
            f"🏋️ **{ex['name']}**\n"
            f"📊 **Схема:** {ex['sets']}\n"
            f"💡 **Биомеханика:** {ex['cue']}"
        )
        try:
            await message.answer_animation(animation=ex["gif"], caption=caption, parse_mode="Markdown")
        except Exception:
            await message.answer(caption, parse_mode="Markdown")

@dp.message(Command("settings"))
async def cmd_settings(message: types.Message):
    save_subscriber(message.chat.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔔 Тест: Утренняя сводка", callback_data="test_morning")],
        [InlineKeyboardButton(text="🥛 Тест: Напоминание в 21:00", callback_data="test_casein")]
    ])
    await message.answer("⚙️ **Автоматические уведомления активны:**\n• `06:00` — Сводка сплита\n• `21:00` — Казеин", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("test_"))
async def handle_test_btn(cb: CallbackQuery):
    if cb.data == "test_morning":
        await send_morning_split()
    elif cb.data == "test_casein":
        await send_casein_reminder()
    await cb.answer("Отправлено!")

@dp.message(F.text & ~F.text.startswith("/"))
async def process_food_text(message: types.Message):
    save_subscriber(message.chat.id)
    status_msg = await message.answer("🔄 Считаю КБЖУ...")
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = (
            f"Рассчитай БЖУ: '{message.text}'. "
            'Ответь ТОЛЬКО валидным JSON без markdown: {"dish_name": "текст", "protein": число_грамм, "calories": число_ккал}'
        )
        resp = await asyncio.to_thread(model.generate_content, prompt)
        data = json.loads(resp.text.strip().replace("```json", "").replace("```", ""))

        total_p, total_c = await asyncio.to_thread(
            sync_log_food_and_get_daily_total, 
            data.get("dish_name", message.text), float(data.get("protein", 0)), float(data.get("calories", 0))
        )
        await status_msg.edit_text(
            format_feedback(data.get("dish_name", message.text), float(data.get("protein", 0)), float(data.get("calories", 0)), total_p, total_c),
            parse_mode="Markdown"
        )
    except Exception:
        await status_msg.edit_text("⚠️ Не удалось разобрать состав. Пример: `3 яйца и протеин`")

@dp.message(F.voice)
async def process_food_voice(message: types.Message):
    save_subscriber(message.chat.id)
    status_msg = await message.answer("🎙️ Слушаю голосовое и считаю КБЖУ...")
    try:
        voice_file = io.BytesIO()
        await bot.download(message.voice, destination=voice_file)
        voice_bytes = voice_file.getvalue()

        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = (
            "Послушай аудио, определи блюдо/продукты и их граммовки. "
            'Ответь ТОЛЬКО валидным JSON без markdown: {"dish_name": "текст", "protein": число_грамм, "calories": число_ккал}'
        )
        resp = await asyncio.to_thread(
            model.generate_content, 
            [{"mime_type": "audio/ogg", "data": voice_bytes}, prompt]
        )
        data = json.loads(resp.text.strip().replace("```json", "").replace("```", ""))

        total_p, total_c = await asyncio.to_thread(
            sync_log_food_and_get_daily_total, 
            data.get("dish_name", "Голосовой ввод"), float(data.get("protein", 0)), float(data.get("calories", 0))
        )
        await status_msg.edit_text(
            format_feedback(data.get("dish_name", "Голосовой ввод"), float(data.get("protein", 0)), float(data.get("calories", 0)), total_p, total_c),
            parse_mode="Markdown"
        )
    except Exception:
        await status_msg.edit_text("⚠️ Не удалось распознать голосовое сообщение.")

@dp.message(F.photo)
async def process_food_photo(message: types.Message):
    save_subscriber(message.chat.id)
    status_msg = await message.answer("🔍 Распознаю блюдо на фото...")
    try:
        photo = message.photo[-1]
        file_io = io.BytesIO()
        await bot.download(photo, destination=file_io)
        file_io.seek(0)
        img = Image.open(file_io)

        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = (
            "Определи блюдо на фото и его примерные КБЖУ. "
            'Ответь ТОЛЬКО валидным JSON без markdown: {"dish_name": "текст", "protein": число_грамм, "calories": число_ккал}'
        )
        resp = await asyncio.to_thread(model.generate_content, [prompt, img])
        data = json.loads(resp.text.strip().replace("```json", "").replace("```", ""))

        total_p, total_c = await asyncio.to_thread(
            sync_log_food_and_get_daily_total, 
            data.get("dish_name", "Блюдо по фото"), float(data.get("protein", 0)), float(data.get("calories", 0))
        )
        await status_msg.edit_text(
            format_feedback(data.get("dish_name", "Блюдо по фото"), float(data.get("protein", 0)), float(data.get("calories", 0)), total_p, total_c),
            parse_mode="Markdown"
        )
    except Exception:
        await status_msg.edit_text("⚠️ Не удалось распознать фото.")

# ----------------- ЗАПУСК -----------------
async def main():
    await start_web_server()
    scheduler.add_job(send_morning_split, CronTrigger(hour=6, minute=0))
    scheduler.add_job(send_casein_reminder, CronTrigger(hour=21, minute=0))
    scheduler.start()
    print("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
