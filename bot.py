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
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1TcDkwfY1R0wrvQQ6PdETSOIzVOvCmjJs4YWvkS-Gkb0")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Переменная окружения BOT_TOKEN не найдена! Проверьте файл .env")

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
            {"name": "Тяга штанги в наклоне
