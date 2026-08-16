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

# Загрузка переменных из .env (если работаете локально)
load_dotenv()

# ----------------- КОНФИГУРАЦИЯ -----------------
# Получаем ключи ТОЛЬКО из переменных окружения
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Ошибка: переменная BOT_TOKEN не установлена в настройках!")

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
            {"name": "Жим лежа", "sets": "4x8-10", "cue": "Сведение лопаток, стабильный мост.", "gif": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Barbell-Bench-Press.gif"},
            {"name": "Армейский жим стоя", "sets": "3x10-12", "cue": "Пресс зажат, гриф до верха груди.", "gif": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Overhead-Press.gif"}
        ]
    },
    "day_b": {
        "title": "День Б (Спина + Бицепс)",
        "exercises": [
            {"name": "Подтягивания", "sets": "4xMax", "cue": "Тяга локтями к тазу.", "gif": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Pull-up.gif"},
            {"name": "Тяга штанги в наклоне", "sets": "4x8-10", "cue": "Корпус 45°, тяга к поясу.", "gif": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Barbell-Bent-Over-Row.gif"}
        ]
    },
    "day_c": {
        "title": "День C (Ноги)",
        "exercises": [
            {"name": "Приседания", "sets": "4x10-12", "cue": "Колени по направлению носков.", "gif": "https://fitnessprogramer.com/wp-content/uploads/2021/02/BARBELL-SQUAT.gif"}
        ]
    }
}

# ----------------- GOOGLE ТАБЛИЦА -----------------
def get_sheets_client():
    creds_json = os.environ.get("GCP_CREDENTIALS")
    if not creds_json:
        return None
    try:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(creds_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        return gspread.authorize(creds)
    except Exception:
        return None

def sync_log_food_and_get_daily_total(meal_name, protein, calories):
    client = get_sheets_client()
    if not client or not SPREADSHEET_ID:
        return protein, calories

    sheet = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = sheet.worksheet("Питание")
    except Exception:
        ws = sheet.add_worksheet(title="Питание", rows=1000, cols=5)
        ws.append_row(["Дата и время", "Блюдо", "Белки", "Калории"])

    ws.append_row([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), meal_name, protein, calories])
    
    # Расчет за сегодня
    all_rows = ws.get_all_values()
    today = datetime.now().strftime("%Y-%m-%d")
    p, c = 0.0, 0.0
    for row in all_rows[1:]:
        if row[0].startswith(today):
            p += float(str(row[2]).replace(",", "."))
            c += float(str(row[3]).replace(",", "."))
    return p, c

# ----------------- ХЕНДЛЕРЫ -----------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    save_subscriber(message.chat.id)
    await message.answer("👋 Привет! Присылай фото еды, текст или голосовое для учета КБЖУ. /workout — тренировки.")

@dp.message(Command("workout"))
async def cmd_workout(message: types.Message):
    day_key = ["day_a", "day_b", "day_c"][datetime.now().day % 3]
    split = SPLIT_PROGRAM[day_key]
    await message.answer(f"📋 **{split['title']}**")
    for ex in split["exercises"]:
        await message.answer_animation(animation=ex["gif"], caption=f"{ex['name']}\n{ex['sets']}\n{ex['cue']}")

@dp.message(F.text & ~F.text.startswith("/"))
async def process_text(message: types.Message):
    status = await message.answer("🔄 Считаю...")
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = f"Блюдо: {message.text}. Ответ в формате JSON: {'dish_name': '...', 'protein': 0.0, 'calories': 0.0}"
        resp = await asyncio.to_thread(model.generate_content, prompt)
        data = json.loads(resp.text.replace("```json", "").replace("```", "").strip())
        
        p, c = await asyncio.to_thread(sync_log_food_and_get_daily_total, data['dish_name'], data['protein'], data['calories'])
        await status.edit_text(f"✅ {data['dish_name']}\nБелки: {p}г\nКалории: {c}ккал")
    except:
        await status.edit_text("⚠️ Ошибка распознавания.")

@dp.message(F.photo)
async def process_photo(message: types.Message):
    status = await message.answer("🔍 Анализ фото...")
    try:
        photo = await bot.download(message.photo[-1])
        img = Image.open(photo)
        model = genai.GenerativeModel("gemini-1.5-flash")
        resp = await asyncio.to_thread(model.generate_content, ["Анализ КБЖУ фото. Ответ JSON: {'dish_name': '...', 'protein': 0.0, 'calories': 0.0}", img])
        data = json.loads(resp.text.replace("```json", "").replace("```", "").strip())
        
        p, c = await asyncio.to_thread(sync_log_food_and_get_daily_total, data['dish_name'], data['protein'], data['calories'])
        await status.edit_text(f"✅ {data['dish_name']}\nБелки: {p}г\nКалории: {c}ккал")
    except:
        await status.edit_text("⚠️ Ошибка фото.")

async def main():
    await start_web_server()
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
