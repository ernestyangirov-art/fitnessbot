import asyncio
import io
import json
import os
import sqlite3
import tempfile
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
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from PIL import Image

load_dotenv()

# ----------------- КОНФИГУРАЦИЯ -----------------
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1TcDkwfY1R0wrvQQ6PdETSOIzVOvCmjJs4YWvkS-Gkb0")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "1yc53-ADTZZBx9hk-WPepohkEZruBXHsS")

DAILY_PROTEIN_TARGET = 150   # г
DAILY_CALORIE_TARGET = 2300  # ккал
DATA_FILE = "subscribers.json"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/spreadsheets"]

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в Environment Variables!")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")

# ----------------- ВЕБ-СЕРВЕР ДЛЯ RENDER -----------------
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

# ----------------- GOOGLE КЛИЕНТЫ -----------------
def get_gcp_creds():
    creds_json = os.environ.get("GCP_CREDENTIALS")
    if not creds_json:
        return None
    try:
        return service_account.Credentials.from_service_account_info(
            json.loads(creds_json), scopes=SCOPES
        )
    except Exception:
        return None

# ----------------- СИНХРОНИЗАЦИЯ GYMUP (.DB -> SHEETS) -----------------
def sync_gymup_task():
    creds = get_gcp_creds()
    if not creds or not SPREADSHEET_ID or not DRIVE_FOLDER_ID:
        return "GCP_CREDENTIALS, SPREADSHEET_ID или DRIVE_FOLDER_ID не настроены."

    drive = build("drive", "v3", credentials=creds)
    res = drive.files().list(
        q=f"'{DRIVE_FOLDER_ID}' in parents and name contains '.db' and trashed=false",
        orderBy="modifiedTime desc",
        pageSize=1,
        fields="files(id, name)"
    ).execute()
    files = res.get("files", [])
    if not files:
        return "В папке не найдены файлы .db"

    req = drive.files().get_media(fileId=files[0]["id"])
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    dl = MediaIoBaseDownload(tmp, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    tmp.close()

    try:
        conn = sqlite3.connect(tmp.name)
        cur = conn.cursor()
        sql = """
        SELECT datetime(t.startDateTime/1000, 'unixepoch', 'localtime'), t.name, e.name,
               ROW_NUMBER() OVER (PARTITION BY w._id ORDER BY s._id),
               s.weight, s.reps, ROUND(s.weight*s.reps, 2),
               CASE WHEN s.reps>0 THEN ROUND(s.weight*(1+s.reps/30.0),1) ELSE s.weight END,
               COALESCE(w.restTime, 0), COALESCE(s.comment, '')
        FROM training t
        JOIN workout w ON w.training_id=t._id
        JOIN th_exercise e ON e._id=w.th_exercise_id
        JOIN set_ s ON s.workout_id=w._id
        ORDER BY t.startDateTime ASC, w.order_num ASC, s._id ASC;
        """
        cur.execute(sql)
        rows = cur.fetchall()
        conn.close()

        sheet = gspread.authorize(creds).open_by_key(SPREADSHEET_ID).sheet1
        rec = sheet.get_all_values()
        existing_keys = set((str(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in rec[1:] if len(r) >= 4)
        new_rows = [list(r) for r in rows if (str(r[0]), str(r[1]), str(r[2]), str(r[3])) not in existing_keys]

        if new_rows:
            sheet.append_rows(new_rows)
            return f"Добавлено {len(new_rows)} новых подходов из GymUp."
        return "Все подходы уже синхронизированы."
    finally:
        if os.path.exists(tmp.name):
            os.remove(tmp.name)

# ----------------- ПИТАНИЕ (GOOGLE ТАБЛИЦА) -----------------
def sync_food_log(dish_name: str, protein: float, calories: float):
    creds = get_gcp_creds()
    if not creds or not SPREADSHEET_ID:
        return protein, calories

    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = sheet.worksheet("Питание")
    except Exception:
        ws = sheet.add_worksheet(title="Питание", rows=1000, cols=5)
        ws.append_row(["Дата и время", "Блюдо/Продукты", "Белки (г)", "Калории (ккал)"])

    now = datetime.now()
    ws.append_row([now.strftime("%Y-%m-%d %H:%M:%S"), dish_name, protein, calories])

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

def format_food_msg(dish_name, protein, calories, total_p, total_c):
    left_p = max(0.0, DAILY_PROTEIN_TARGET - total_p)
    return (
        f"✅ **Питание учтено:**\n"
        f"🍽️ *{dish_name}* (+{protein:.1f}г белка, +{calories:.0f} ккал)\n\n"
        f"🥩 **Белок за сегодня:** {total_p:.0f} / {DAILY_PROTEIN_TARGET} г\n"
        f"🔥 **Калории:** {total_c:.0f} / {DAILY_CALORIE_TARGET} ккал\n"
        f"⏳ **Осталось добрать:** {left_p:.0f} г белка"
    )

# ----------------- БАЗА УПРАЖНЕНИЙ -----------------
SPLIT_PROGRAM = {
    "day_a": {
        "title": "День А (Грудь + Плечи + Трицепс)",
        "exercises": [
            {"name": "Жим лежа", "sets": "4x8-10", "cue": "Сведение лопаток, мост, угол локтей ~75°.", "gif": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Barbell-Bench-Press.gif"},
            {"name": "Армейский жим стоя", "sets": "3x10-12", "cue": "Пресс зажат, гриф до верха груди.", "gif": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Overhead-Press.gif"}
        ]
    },
    "day_b": {
        "title": "День Б (Спина + Бицепс)",
        "exercises": [
            {"name": "Подтягивания", "sets": "4xMax", "cue": "Тяга локтями к тазу, контроль внизу.", "gif": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Pull-up.gif"},
            {"name": "Тяга штанги в наклоне", "sets": "4x8-10", "cue": "Корпус 45°, тяга к поясу.", "gif": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Barbell-Bent-Over-Row.gif"},
            {"name": "Молотковые сгибания", "sets": "3x12", "cue": "Нейтральный хват, локти прижаты.", "gif": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Hammer-Curls.gif"}
        ]
    },
    "day_c": {
        "title": "День C (Ноги)",
        "exercises": [
            {"name": "Приседания", "sets": "4x10-12", "cue": "Колени по направлению носков.", "gif": "https://fitnessprogramer.com/wp-content/uploads/2021/02/BARBELL-SQUAT.gif"}
        ]
    }
}

# ----------------- ХЕНДЛЕРЫ -----------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    save_subscriber(message.chat.id)
    await message.answer(
        "👋 **Бот активен!**\n\n"
        "🥗 **Питание:** отправьте текст, фото блюда или голосовое.\n"
        "🏋️ **Тренировки:** `/workout` — план с техникой и GIF.\n"
        "🔄 **Синхронизация:** `/sync` — вручную загрузить тренировки из GymUp."
    )

@dp.message(Command("sync"))
async def cmd_sync(message: types.Message):
    msg = await message.answer("⏳ Проверяю бэкапы GymUp на Google Drive...")
    res = await asyncio.to_thread(sync_gymup_task)
    await msg.edit_text(f"🔄 **Результат синхронизации:**\n{res}")

@dp.message(Command("workout"))
async def cmd_workout(message: types.Message):
    day_key = ["day_a", "day_b", "day_c"][datetime.now().day % 3]
    split = SPLIT_PROGRAM[day_key]
    await message.answer(f"📋 **План: {split['title']}**")
    for ex in split["exercises"]:
        try:
            await message.answer_animation(animation=ex["gif"], caption=f"🏋️ {ex['name']}\n📊 {ex['sets']}\n💡 {ex['cue']}")
        except Exception:
            await message.answer(f"🏋️ {ex['name']}\n📊 {ex['sets']}\n💡 {ex['cue']}")

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_food_text(message: types.Message):
    save_subscriber(message.chat.id)
    msg = await message.answer("🔄 Считаю КБЖУ...")
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        resp = await asyncio.to_thread(model.generate_content, f"Рассчитай БЖУ: '{message.text}'. JSON: {{'dish_name': '...', 'protein': 0.0, 'calories': 0.0}}")
        data = json.loads(resp.text.replace("```json", "").replace("```", "").strip())
        dish, p, c = data.get("dish_name", message.text), float(data.get("protein", 0)), float(data.get("calories", 0))
        tot_p, tot_c = await asyncio.to_thread(sync_food_log, dish, p, c)
        await msg.edit_text(format_food_msg(dish, p, c, tot_p, tot_c), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text("⚠️ Ошибка обработки текста.")

@dp.message(F.photo)
async def handle_food_photo(message: types.Message):
    save_subscriber(message.chat.id)
    msg = await message.answer("🔍 Распознаю фото...")
    try:
        photo_bytes = io.BytesIO()
        await bot.download(message.photo[-1], destination=photo_bytes)
        img = Image.open(photo_bytes)
        model = genai.GenerativeModel("gemini-1.5-flash")
        resp = await asyncio.to_thread(model.generate_content, ["Определи блюдо и посчитай КБЖУ. Ответ JSON: {'dish_name': '...', 'protein': 0.0, 'calories': 0.0}", img])
        data = json.loads(resp.text.replace("```json", "").replace("```", "").strip())
        dish, p, c = data.get("dish_name", "Блюдо по фото"), float(data.get("protein", 0)), float(data.get("calories", 0))
        tot_p, tot_c = await asyncio.to_thread(sync_food_log, dish, p, c)
        await msg.edit_text(format_food_msg(dish, p, c, tot_p, tot_c), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text("⚠️ Ошибка распознавания фото.")

# ----------------- ЗАПУСК -----------------
async def main():
    await start_web_server()
    # Авто-синхронизация GymUp каждый час
    scheduler.add_job(lambda: asyncio.create_task(asyncio.to_thread(sync_gymup_task)), CronTrigger(minute=0))
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
