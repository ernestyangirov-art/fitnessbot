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
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton
)
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
SETTINGS_FILE = "user_settings.json"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/spreadsheets"]

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан!")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")

# ----------------- НАСТРОЙКИ -----------------
def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_settings(data):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_user_config(chat_id):
    settings = load_settings()
    cid = str(chat_id)
    if cid not in settings:
        settings[cid] = {"morning_notify": True, "casein_notify": True}
        save_settings(settings)
    return settings[cid]

def update_user_config(chat_id, key, value):
    settings = load_settings()
    cid = str(chat_id)
    if cid not in settings:
        settings[cid] = {"morning_notify": True, "casein_notify": True}
    settings[cid][key] = value
    save_settings(settings)

# ----------------- БАЗА УПРАЖНЕНИЙ С АНАТОМИЧЕСКИМИ АНИМАЦИЯМИ -----------------
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
                "mistake": "Разведение локтей на 90°, отрыв таза."
            },
            {
                "name": "Армейский жим стоя",
                "sets": "3x10-12 (RIR 1-2)",
                "media": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Overhead-Press.gif",
                "setup": "Хват чуть шире плеч, ягодицы и пресс в жестком замке, нейтральная поясница.",
                "execution": "Траектория грифа строго вертикальная, голова пропускает гриф и возвращается в нейтраль.",
                "mistake": "Прогиб в пояснице, толчок ногами."
            }
        ]
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
                "mistake": "Рывки ногами, неполная амплитуда."
            },
            {
                "name": "Тяга штанги в наклоне",
                "sets": "4x8-10 (RIR 1-2)",
                "media": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Barbell-Bent-Over-Row.gif",
                "setup": "Наклон корпуса 45-60°, колени мягкие, позвоночник нейтрален.",
                "execution": "Тяга грифа вдоль бедер к низу живота за счет локтей и сведения лопаток.",
                "mistake": "Инерция корпусом, подтягивание веса к груди силой рук."
            },
            {
                "name": "Молотковые сгибания",
                "sets": "3x12 (RIR 1)",
                "media": "https://fitnessprogramer.com/wp-content/uploads/2021/02/Hammer-Curls.gif",
                "setup": "Нейтральный хват, локти зафиксированы строго по бокам корпуса.",
                "execution": "Подъем без читинга, пауза 1 сек в пиковом сокращении брахиалиса.",
                "mistake": "Заброс веса спиной, вынос локтей вперед."
            }
        ]
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
                "mistake": "Сведение коленей внутрь, клевок тазом."
            }
        ]
    }
}

# ----------------- КЛАВИАТУРЫ -----------------
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏋️ Тренировка дня"), KeyboardButton(text="📊 Аналитика")],
            [KeyboardButton(text="🔄 Синхронизация GymUp"), KeyboardButton(text="⚙️ Настройки")]
        ],
        resize_keyboard=True
    )

def get_settings_keyboard(chat_id):
    cfg = get_user_config(chat_id)
    m_status = "✅ Вкл" if cfg.get("morning_notify", True) else "❌ Выкл"
    c_status = "✅ Вкл" if cfg.get("casein_notify", True) else "❌ Выкл"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🌅 Утренний сплит (06:00): {m_status}", callback_data="toggle_morning")],
            [InlineKeyboardButton(text=f"🥛 Казеин (21:00): {c_status}", callback_data="toggle_casein")],
            [InlineKeyboardButton(text="🔔 Тест утреннего пуша", callback_data="test_morning_push")],
            [InlineKeyboardButton(text="🔔 Тест казеинового пуша", callback_data="test_casein_push")]
        ]
    )

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
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# ----------------- GOOGLE & GYMUP СИНХРОНИЗАЦИЯ -----------------
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

def sync_gymup_task():
    creds = get_gcp_creds()
    if not creds or not SPREADSHEET_ID or not DRIVE_FOLDER_ID:
        return "⚠️ GCP_CREDENTIALS или ID таблицы не настроены."

    drive = build("drive", "v3", credentials=creds)
    res = drive.files().list(
        q=f"'{DRIVE_FOLDER_ID}' in parents and name contains '.db' and trashed=false",
        orderBy="modifiedTime desc",
        pageSize=1,
        fields="files(id, name)"
    ).execute()
    files = res.get("files", [])
    if not files:
        return "В папке Drive не найден бэкап .db"

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
            return f"✅ Добавлено {len(new_rows)} новых подходов из бэкапа `{files[0]['name']}`."
        return f"👌 Все данные актуальны. Новых записей нет."
    finally:
        if os.path.exists(tmp.name):
            os.remove(tmp.name)

# ----------------- НАУЧНАЯ АНАЛИТИКА ТРЕНИРОВОК -----------------
def get_scientific_analytics():
    creds = get_gcp_creds()
    if not creds or not SPREADSHEET_ID:
        return "⚠️ Нет доступа к Google Таблице."

    sheet = gspread.authorize(creds).open_by_key(SPREADSHEET_ID).sheet1
    rows = sheet.get_all_values()
    if len(rows) <= 1:
        return "Таблица пуста. Выполните сначала синхронизацию."

    # Группируем по уникальным тренировкам
    trainings = {}
    for r in rows[1:]:
        if len(r) < 8 or not r[0]:
            continue
        dt_str = r[0].split(" ")[0]
        prog = r[1] if r[1] else "Тренировка"
        t_key = (dt_str, prog)
        if t_key not in trainings:
            trainings[t_key] = []
        trainings[t_key].append(r)

    if not trainings:
        return "Недостаточно данных для анализа."

    sorted_sessions = sorted(trainings.keys(), key=lambda x: x[0])
    last_session_key = sorted_sessions[-1]
    last_date, last_prog = last_session_key
    last_rows = trainings[last_session_key]

    # Ищем предыдущую сессию с аналогичной программой для оценки прогрессии нагрузки
    prev_rows = None
    for s_key in reversed(sorted_sessions[:-1]):
        if s_key[1] == last_prog:
            prev_rows = trainings[s_key]
            break

    # Метрики последней тренировки
    total_volume = sum(float(str(r[6]).replace(",", ".")) for r in last_rows if r[6])
    hard_sets = len([r for r in last_rows if int(r[5]) >= 5]) if all(r[5].isdigit() for r in last_rows if r[5]) else len(last_rows)

    # Классификация по мышечным паттернам
    muscle_volume = {"Тяговые (Спина/Бицепс)": 0, "Жимовые (Грудь/Плечи/Трицепс)": 0, "Ноги/Кор": 0}
    for r in last_rows:
        ex = r[2].lower()
        if any(k in ex for k in ["тяга", "подтягивания", "бицепс", "блок"]):
            muscle_volume["Тяговые (Спина/Бицепс)"] += 1
        elif any(k in ex for k in ["жим", "брусья", "трицепс", "разводка"]):
            muscle_volume["Жимовые (Грудь/Плечи/Трицепс)"] += 1
        elif any(k in ex for k in ["присед", "выпады", "становая", "пресс", "голень"]):
            muscle_volume["Ноги/Кор"] += 1

    # Анализ прогрессивной перегрузки (Progressive Overload)
    overload_text = ""
    if prev_rows:
        prev_vol = sum(float(str(r[6]).replace(",", ".")) for r in prev_rows if r[6])
        diff = total_volume - prev_vol
        pct = (diff / prev_vol * 100) if prev_vol > 0 else 0
        sign = "+" if diff >= 0 else ""
        overload_text = (
            f"\n📈 **Прогрессия относительно прошлой сессии:**\n"
            f"• Изменение тоннажа: `{sign}{diff:,.0f} кг` (`{sign}{pct:.1f}%`)\n"
            f"• Статус стимула: `{'🔥 Прогрессивная перегрузка достигнута' if diff > 0 else '⚠️ Разгрузка / поддержка'}`\n"
        )

    # Оценка качества объема (Junk Volume Warning)
    junk_warn = ""
    max_muscle_sets = max(muscle_volume.values()) if muscle_volume.values() else 0
    if max_muscle_sets > 12:
        junk_warn = "\n⚠️ **Внимание:** >12 подходов на одну мышечную группу за сессию. Риск избыточного утомления (Junk Volume)."

    # Рекорды 1ПМ за тренировку
    prs = {}
    for r in last_rows:
        ex = r[2]
        try:
            val = float(str(r[7]).replace(",", "."))
            if ex not in prs or val > prs[ex]:
                prs[ex] = val
        except Exception:
            pass

    prs_formatted = "\n".join([f"  └ *{k}:* `1ПМ ~ {v:.1f} кг`" for k, v in prs.items()])

    return (
        f"🔬 **Научно обоснованная аналитика сессии**\n"
        f"📅 **Дата:** `{last_date}` | **Сплит:** `{last_prog}`\n\n"
        f"⚡ **Стимул гипертрофии & Объем:**\n"
        f"• Эффективных рабочих подходов: `{hard_sets}`\n"
        f"• Суммарный тоннаж (Volume Load): `{total_volume:,.0f} кг`\n"
        f"{overload_text}"
        f"🧬 **Распределение стимула по паттернам:**\n"
        + "\n".join([f"• {k}: `{v}` подходов" for k, v in muscle_volume.items() if v > 0]) +
        f"\n{junk_warn}\n\n"
        f"🎯 **Пиковая производительность (Расчетный 1ПМ):**\n{prs_formatted}"
    )

# ----------------- ПИТАНИЕ -----------------
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

def render_progress_bar(current, target, length=10):
    ratio = min(1.0, max(0.0, current / target)) if target > 0 else 0
    filled = int(ratio * length)
    bar = "█" * filled + "░" * (length - filled)
    percent = int(ratio * 100)
    return f"[{bar}] {percent}%"

def format_food_feedback(dish_name, protein, calories, total_p, total_c):
    left_p = max(0.0, DAILY_PROTEIN_TARGET - total_p)
    p_bar = render_progress_bar(total_p, DAILY_PROTEIN_TARGET)
    c_bar = render_progress_bar(total_c, DAILY_CALORIE_TARGET)

    return (
        f"✅ **Прием пищи зафиксирован:**\n"
        f"🍽️ *{dish_name}*\n"
        f"➕ `+{protein:.1f} г белка` | `+{calories:.0f} ккал`\n\n"
        f"🥩 **Белок:** {total_p:.0f} / {DAILY_PROTEIN_TARGET} г\n"
        f"   └ `{p_bar}`\n"
        f"🔥 **Калории:** {total_c:.0f} / {DAILY_CALORIE_TARGET} ккал\n"
        f"   └ `{c_bar}`\n\n"
        f"⏳ **Осталось добрать белка:** `{left_p:.0f} г`"
    )

# ----------------- НАПОМИНАНИЯ -----------------
async def send_morning_split():
    settings = load_settings()
    day_key = ["day_a", "day_b", "day_c"][datetime.now().day % 3]
    split = SPLIT_PROGRAM[day_key]

    msg = (
        f"🌅 **Утренняя сводка тренировок**\n\n"
        f"🎯 Сегодня по плану: **{split['title']}**\n"
        f"🥩 Суточная цель по белку: **{DAILY_PROTEIN_TARGET} г**\n\n"
        f"Нажмите кнопку *«🏋️ Тренировка дня»* для просмотра биомеханики и анатомических карточек."
    )

    for cid, cfg in settings.items():
        if cfg.get("morning_notify", True):
            try:
                await bot.send_message(chat_id=int(cid), text=msg, parse_mode="Markdown")
            except Exception:
                pass

async def send_casein_reminder():
    settings = load_settings()
    msg = (
        f"🥛 **21:00 — Вечерний чек-ин!**\n\n"
        f"Время закрыть суточную норму белка казеином перед сном для ночного синтеза мышечного белка."
    )

    for cid, cfg in settings.items():
        if cfg.get("casein_notify", True):
            try:
                await bot.send_message(chat_id=int(cid), text=msg, parse_mode="Markdown")
            except Exception:
                pass

# ----------------- ХЕНДЛЕРЫ -----------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    get_user_config(message.chat.id)
    await message.answer(
        "👋 **Фитнес-хаб активен.**\n\n"
        "🥗 Отправляйте фото еды, надиктовывайте голосом или пишите текстом.\n"
        "🏋️ Тренировочные карточки, аналитика и синхронизация — в нижнем меню.",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

@dp.message(F.text == "🏋️ Тренировка дня")
@dp.message(Command("workout"))
async def show_workout(message: types.Message):
    day_key = ["day_a", "day_b", "day_c"][datetime.now().day % 3]
    split = SPLIT_PROGRAM[day_key]

    await message.answer(f"📋 **ПЛАН: {split['title']}**\n" + "—" * 20, parse_mode="Markdown")

    for ex in split["exercises"]:
        card = (
            f"🏋️ **{ex['name']}**\n"
            f"📊 **Схема:** `{ex['sets']}`\n\n"
            f"📐 **Исходное положение & Настройка:**\n{ex['setup']}\n\n"
            f"🎯 **Биомеханика движения:**\n{ex['execution']}\n\n"
            f"⚠️ **Критическая ошибка:**\n{ex['mistake']}"
        )
        try:
            await message.answer_animation(animation=ex["media"], caption=card, parse_mode="Markdown")
        except Exception:
            await message.answer(card, parse_mode="Markdown")

@dp.message(F.text == "📊 Аналитика")
@dp.message(Command("analytics"))
async def show_analytics(message: types.Message):
    status_msg = await message.answer("🔬 Провожу научно обоснованный расчет сессии...")
    res = await asyncio.to_thread(get_scientific_analytics)
    await status_msg.edit_text(res, parse_mode="Markdown")

@dp.message(F.text == "🔄 Синхронизация GymUp")
@dp.message(Command("sync"))
async def handle_sync_btn(message: types.Message):
    status_msg = await message.answer("⏳ Скачиваю бэкап SQLite с Google Drive...")
    res = await asyncio.to_thread(sync_gymup_task)
    await status_msg.edit_text(f"🔄 **Результат:**\n{res}", parse_mode="Markdown")

@dp.message(F.text == "⚙️ Настройки")
@dp.message(Command("settings"))
async def show_settings(message: types.Message):
    await message.answer(
        "⚙️ **Управление уведомлениями:**",
        reply_markup=get_settings_keyboard(message.chat.id),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("toggle_"))
async def handle_toggle(cb: CallbackQuery):
    cfg = get_user_config(cb.message.chat.id)
    if cb.data == "toggle_morning":
        update_user_config(cb.message.chat.id, "morning_notify", not cfg.get("morning_notify", True))
    elif cb.data == "toggle_casein":
        update_user_config(cb.message.chat.id, "casein_notify", not cfg.get("casein_notify", True))
    
    await cb.message.edit_reply_markup(reply_markup=get_settings_keyboard(cb.message.chat.id))
    await cb.answer("Обновлено!")

@dp.callback_query(F.data.startswith("test_"))
async def handle_test_pushes(cb: CallbackQuery):
    if cb.data == "test_morning_push":
        await send_morning_split()
    elif cb.data == "test_casein_push":
        await send_casein_reminder()
    await cb.answer("Отправлено!")

# ----------------- ПИТАНИЕ: ТЕКСТ / ГОЛОС / ФОТО -----------------
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_food_text(message: types.Message):
    if message.text in ["🏋️ Тренировка дня", "📊 Аналитика", "🔄 Синхронизация GymUp", "⚙️ Настройки"]:
        return

    status_msg = await message.answer("🔄 Анализирую состав...")
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = (
            f"Рассчитай БЖУ продукта/блюда: '{message.text}'. "
            'Ответь ТОЛЬКО валидным JSON: {"dish_name": "текст", "protein": число_грамм, "calories": число_ккал}'
        )
        resp = await asyncio.to_thread(model.generate_content, prompt)
        clean = resp.text.strip().replace("```json", "").replace("```", "")
        data = json.loads(clean)

        dish = data.get("dish_name", message.text)
        p = float(data.get("protein", 0))
        c = float(data.get("calories", 0))

        tot_p, tot_c = await asyncio.to_thread(sync_food_log, dish, p, c)
        await status_msg.edit_text(format_food_feedback(dish, p, c, tot_p, tot_c), parse_mode="Markdown")
    except Exception:
        await status_msg.edit_text("⚠️ Не удалось разобрать состав.")

@dp.message(F.voice)
async def handle_food_voice(message: types.Message):
    status_msg = await message.answer("🎙️ Распознаю голос и считаю БЖУ...")
    try:
        voice_file = io.BytesIO()
        await bot.download(message.voice, destination=voice_file)
        voice_file.seek(0)
        voice_bytes = voice_file.getvalue()

        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = (
            "Послушай аудиозапись, определи продукты и их граммовки. "
            'Ответь ТОЛЬКО валидным JSON: {"dish_name": "название", "protein": число_грамм, "calories": число_ккал}'
        )
        resp = await asyncio.to_thread(
            model.generate_content,
            [{"mime_type": "audio/ogg", "data": voice_bytes}, prompt]
        )
        clean = resp.text.strip().replace("```json", "").replace("```", "")
        data = json.loads(clean)

        dish = data.get("dish_name", "Голосовой ввод")
        p = float(data.get("protein", 0))
        c = float(data.get("calories", 0))

        tot_p, tot_c = await asyncio.to_thread(sync_food_log, dish, p, c)
        await status_msg.edit_text(format_food_feedback(dish, p, c, tot_p, tot_c), parse_mode="Markdown")
    except Exception:
        await status_msg.edit_text("⚠️ Не удалось распознать голос.")

@dp.message(F.photo)
async def handle_food_photo(message: types.Message):
    status_msg = await message.answer("🔍 Распознаю этикетку/блюдо...")
    try:
        photo_bytes = io.BytesIO()
        await bot.download(message.photo[-1], destination=photo_bytes)
        photo_bytes.seek(0)
        img = Image.open(photo_bytes)

        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = (
            "Ты спортивный нутрициолог. Внимательно распознай этикетку или блюдо на фото. "
            "Найди пищевую ценность (белки и калории) на всю банку/порцию или пересчитай на указанный вес. "
            'Ответь СТРОГО валидным JSON без markdown: {"dish_name": "название продукта", "protein": число_грамм, "calories": число_ккал}'
        )
        resp = await asyncio.to_thread(model.generate_content, [prompt, img])
        clean = resp.text.strip().replace("```json", "").replace("```", "")
        data = json.loads(clean)

        dish = data.get("dish_name", "Блюдо по фото")
        p = float(data.get("protein", 0))
        c = float(data.get("calories", 0))

        tot_p, tot_c = await asyncio.to_thread(sync_food_log, dish, p, c)
        await status_msg.edit_text(format_food_feedback(dish, p, c, tot_p, tot_c), parse_mode="Markdown")
    except Exception as e:
        await status_msg.edit_text(f"⚠️ Ошибка обработки: {e}")

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
