import asyncio
import io
import json
import os
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
import google.generativeai as genai
import gspread
from google.oauth2 import service_account
from PIL import Image

load_dotenv()

# --- КОНФИГУРАЦИЯ ---
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN", "8867533906:AAHfN9ZtDWlurbjk4mhWj-bF2ZMlREPF0JA")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1TcDkwfY1R0wrvQQ6PdETSOIzVOvCmjJs4YWvkS-Gkb0")
DATA_FILE = "subscribers.json"

genai.configure(api_key=GEMINI_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")

def load_subscribers():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f: return set(json.load(f))
        except: return set()
    return set()

def save_subscriber(chat_id):
    subscribers = load_subscribers()
    if chat_id not in subscribers:
        subscribers.add(chat_id)
        with open(DATA_FILE, "w") as f: json.dump(list(subscribers), f)

# --- ЛОГИКА ---
async def main():
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
