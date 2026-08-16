import asyncio
import io
import json
import os
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
from google import genai
from google.genai import types as genai_types
import gspread
from google.oauth2 import service_account
from PIL import Image

load_dotenv()

# ----------------- КОНФИГУРАЦИЯ -----------------
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1TcDkwfY1R0wrvQQ6PdETSOIzVOvCmjJs4YWvkS-Gkb0")

DAILY_PROTEIN_TARGET = 150   # г
DAILY_CALORIE_TARGET = 2300  # ккал

TRAINING_SHEET = "Тренировки"
SETTINGS_SHEET = "Настройки"
FOOD_SHEET = "Питание"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан!")

gemini = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")

# Колонки листа «Тренировки»
COL_DATE, COL_SPLIT, COL_EXERCISE, COL_EX_ID = 1, 3, 4, 5
COL_VOLUME, COL_ORM, COL_HARD = 9, 11, 12

# Паттерн движения по id упражнения.
EXERCISE_PATTERNS = {
    107: "Грудь", 112: "Грудь", 121: "Грудь", 139: "Грудь", 147: "Грудь",
    151: "Грудь",
    103: "Кардио", 171: "Кардио", 235: "Кардио",
    403: "Кор", 411: "Кор", 418: "Кор", 434: "Кор", 441: "Кор",
    7: "Ноги", 31: "Ноги", 39: "Ноги", 164: "Ноги", 179: "Ноги",
    180: "Ноги", 191: "Ноги", 220: "Ноги", 284: "Ноги", 567: "Ноги",
    289: "Плечи", 291: "Плечи", 292: "Плечи", 323: "Плечи", 342: "Плечи",
    345: "Плечи", 356: "Плечи",
    571: "Разминка",
    53: "Руки", 55: "Руки", 57: "Руки", 66: "Руки", 94: "Руки",
    493: "Руки", 496: "Руки", 498: "Руки", 500: "Руки", 502: "Руки",
    503: "Руки", 509: "Руки", 526: "Руки", 533: "Руки",
    452: "Тяги", 454: "Тяги", 463: "Тяги", 465: "Тяги", 471: "Тяги",
    540: "Тяги", 542: "Тяги", 544: "Тяги", 554: "Тяги",
}

WEEKLY_SET_LIMIT = 20


# ----------------- GOOGLE -----------------
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


def open_sheet(title, headers=None, cols=10):
    creds = get_gcp_creds()
    if not creds or not SPREADSHEET_ID:
        return None
    book = gspread.authorize(creds).open_by_key(SPREADSHEET_ID)
    try:
        return book.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = book.add_worksheet(title=title, rows=1000, cols=cols)
        if headers:
            ws.append_row(headers)
        return ws


# ----------------- НАСТРОЙКИ (в Google Таблице) -----------------
SETTINGS_HEADERS = ["chat_id", "morning_notify", "casein_notify"]
DEFAULT_CONFIG = {"morning_notify": True, "casein_notify": True}

_settings_cache = None


def _parse_flag(value):
    return str(value).strip().upper() in ("TRUE", "1", "ДА", "YES")


def load_settings(force=False):
    global _settings_cache
    if _settings_cache is not None and not force:
        return _settings_cache

    ws = open_sheet(SETTINGS_SHEET, SETTINGS_HEADERS, cols=3)
    result = {}
    if ws:
        try:
            for row in ws.get_all_values()[1:]:
                if row and row[0]:
                    result[str(row[0])] = {
                        "morning_notify": _parse_flag(row[1]) if len(row) > 1 else True,
                        "casein_notify": _parse_flag(row[2]) if len(row) > 2 else True,
                    }
        except Exception:
            pass
    _settings_cache = result
    return result


def get_user_config(chat_id):
    settings = load_settings()
    cid = str(chat_id)
    if cid not in settings:
        settings[cid] = dict(DEFAULT_CONFIG)
        _persist_user(cid, settings[cid])
    return settings[cid]


def update_user_config(chat_id, key, value):
    settings = load_settings()
    cid = str(chat_id)
    cfg = settings.setdefault(cid, dict(DEFAULT_CONFIG))
    cfg[key] = value
    _persist_user(cid, cfg)
    return cfg


def _persist_user(cid, cfg):
    ws = open_sheet(SETTINGS_SHEET, SETTINGS_HEADERS, cols=3)
    if not ws:
        return
    row = [cid, str(cfg["morning_notify"]).upper(), str(cfg["casein_notify"]).upper()]
    try:
        for idx, existing in enumerate(ws.get_all_values(), start=1):
            if existing and existing[0] == cid:
                ws.update(range_name=f"A{idx}:C{idx}", values=[row])
                return
        ws.append_row(row)
    except Exception:
        pass


# ----------------- БАЗА УПРАЖНЕНИЙ -----------------
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
            [KeyboardButton(text="⚙️ Настройки")]
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


# ----------------- АНАЛИТИКА ТРЕНИРОВОК -----------------
def _num(value):
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return 0.0


def _pattern(row):
    try:
        return EXERCISE_PATTERNS.get(int(_num(row[COL_EX_ID])), "Прочее")
    except (IndexError, ValueError):
        return "Прочее"


def build_analytics(rows):
    sessions = {}
    for r in rows:
        if len(r) <= COL_HARD or not r[COL_DATE]:
            continue
        sessions.setdefault(r[COL_DATE], []).append(r)

    if not sessions:
        return "Недостаточно данных для анализа."

    order = sorted(sessions)
    last_key = order[-1]
    last_rows = sessions[last_key]
    last_split = last_rows[0][COL_SPLIT] or "Тренировка"

    volume = sum(_num(r[COL_VOLUME]) for r in last_rows)
    hard_sets = sum(1 for r in last_rows if _num(r[COL_HARD]) >= 4)

    per_pattern = {}
    for r in last_rows:
        p = _pattern(r)
        if p != "Разминка":
            per_pattern[p] = per_pattern.get(p, 0) + 1

    overload = ""
    for key in reversed(order[:-1]):
        prev = sessions[key]
        if (prev[0][COL_SPLIT] or "Тренировка") == last_split:
            prev_volume = sum(_num(r[COL_VOLUME]) for r in prev)
            diff = volume - prev_volume
            pct = (diff / prev_volume * 100) if prev_volume > 0 else 0
            sign = "+" if diff >= 0 else ""
            status = "🔥 Перегрузка достигнута" if diff > 0 else "⚠️ Разгрузка или откат"
            overload = (
                f"\n📈 **Против прошлой «{last_split}»** ({key[:10]})\n"
                f"• Тоннаж: `{sign}{diff:,.0f} кг` (`{sign}{pct:.1f}%`)\n"
                f"• {status}\n"
            )
            break

    weekly = {}
    try:
        last_dt = datetime.strptime(last_key[:10], "%Y-%m-%d")
        for key, rws in sessions.items():
            day = datetime.strptime(key[:10], "%Y-%m-%d")
            if 0 <= (last_dt - day).days < 7:
                for r in rws:
                    p = _pattern(r)
                    if p != "Разминка":
                        weekly[p] = weekly.get(p, 0) + 1
    except ValueError:
        weekly = {}

    overload_warn = ""
    hot = [f"{k} ({v})" for k, v in weekly.items() if v > WEEKLY_SET_LIMIT]
    if hot:
        overload_warn = (
            f"\n⚠️ **За 7 дней выше ориентира {WEEKLY_SET_LIMIT} подходов:** "
            + ", ".join(hot)
        )

    records = {}
    for r in last_rows:
        orm = _num(r[COL_ORM])
        if orm > 0:
            name = r[COL_EXERCISE]
            if orm > records.get(name, 0):
                records[name] = orm

    records_text = "\n".join(
        f"  └ *{k}:* `~{v:.1f} кг`"
        for k, v in sorted(records.items(), key=lambda x: -x[1])[:5]
    ) or "  └ нет упражнений с отягощением"

    pattern_text = "\n".join(
        f"• {k}: `{v}` подх. (за неделю `{weekly.get(k, v)}`)"
        for k, v in sorted(per_pattern.items(), key=lambda x: -x[1])
    )

    return (
        f"🔬 **Аналитика сессии**\n"
        f"📅 `{last_key}` | **{last_split}**\n\n"
        f"⚡ **Объём**\n"
        f"• Тяжёлых подходов (тяжесть 4-5): `{hard_sets}` из `{len(last_rows)}`\n"
        f"• Тоннаж железа: `{volume:,.0f} кг`\n"
        f"{overload}\n"
        f"🧬 **Паттерны движения**\n{pattern_text}"
        f"{overload_warn}\n\n"
        f"🎯 **Расчётный 1ПМ**\n{records_text}"
    )


def get_training_analytics():
    ws = open_sheet(TRAINING_SHEET)
    if not ws:
        return "⚠️ Нет доступа к Google Таблице."
    try:
        rows = ws.get_all_values()
    except Exception:
        return "⚠️ Не удалось прочитать лист «Тренировки»."
    if len(rows) <= 1:
        return "Лист «Тренировки» пуст. Дождитесь синхронизации GymUp."
    return build_analytics(rows[1:])


# ----------------- ПИТАНИЕ -----------------
def ask_gemini_json(parts):
    if not gemini:
        raise RuntimeError("GEMINI_API_KEY не задан")
    resp = gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=parts,
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json"
        ),
    )
    text = (resp.text or "").strip().replace("```json", "").replace("```", "")
    return json.loads(text)


def sync_food_log(dish_name, protein, calories):
    ws = open_sheet(
        FOOD_SHEET,
        ["Дата и время", "Блюдо/Продукты", "Белки (г)", "Калории (ккал)"],
        cols=5,
    )
    if not ws:
        return protein, calories

    now = datetime.now()
    ws.append_row([now.strftime("%Y-%m-%d %H:%M:%S"), dish_name, protein, calories])

    today = now.strftime("%Y-%m-%d")
    total_p, total_c = 0.0, 0.0
    for row in ws.get_all_values()[1:]:
        if len(row) >= 4 and row[0].startswith(today):
            total_p += _num(row[2])
            total_c += _num(row[3])
    return total_p, total_c


def render_progress_bar(current, target, length=10):
    ratio = min(1.0, max(0.0, current / target)) if target > 0 else 0
    filled = int(ratio * length)
    return f"[{'█' * filled}{'░' * (length - filled)}] {int(ratio * 100)}%"


def format_food_feedback(dish_name, protein, calories, total_p, total_c):
    left_p = max(0.0, DAILY_PROTEIN_TARGET - total_p)
    return (
        f"✅ **Приём пищи зафиксирован:**\n"
        f"🍽️ *{dish_name}*\n"
        f"➕ `+{protein:.1f} г белка` | `+{calories:.0f} ккал`\n\n"
        f"🥩 **Белок:** {total_p:.0f} / {DAILY_PROTEIN_TARGET} г\n"
        f"   └ `{render_progress_bar(total_p, DAILY_PROTEIN_TARGET)}`\n"
        f"🔥 **Калории:** {total_c:.0f} / {DAILY_CALORIE_TARGET} ккал\n"
        f"   └ `{render_progress_bar(total_c, DAILY_CALORIE_TARGET)}`\n\n"
        f"⏳ **Осталось добрать белка:** `{left_p:.0f} г`"
    )


async def log_and_reply(status_msg, data, fallback_name):
    dish = data.get("dish_name", fallback_name)
    p = float(data.get("protein", 0) or 0)
    c = float(data.get("calories", 0) or 0)
    tot_p, tot_c = await asyncio.to_thread(sync_food_log, dish, p, c)
    await status_msg.edit_text(
        format_food_feedback(dish, p, c, tot_p, tot_c), parse_mode="Markdown"
    )


# ----------------- НАПОМИНАНИЯ -----------------
async def send_morning_split():
    settings = await asyncio.to_thread(load_settings, True)
    day_key = ["day_a", "day_b", "day_c"][datetime.now().day % 3]
    split = SPLIT_PROGRAM[day_key]

    msg = (
        f"🌅 **Утренняя сводка тренировок**\n\n"
        f"🎯 Сегодня по плану: **{split['title']}**\n"
        f"🥩 Суточная цель по белку: **{DAILY_PROTEIN_TARGET} г**\n\n"
        f"Нажми *«🏋️ Тренировка дня»* для карточек с биомеханикой."
    )

    for cid, cfg in settings.items():
        if cfg.get("morning_notify", True):
            try:
                await bot.send_message(chat_id=int(cid), text=msg, parse_mode="Markdown")
            except Exception:
                pass


async def send_casein_reminder():
    settings = await asyncio.to_thread(load_settings, True)
    msg = (
        "🥛 **21:00 — Вечерний чек-ин!**\n\n"
        "Время закрыть суточную норму белка перед сном."
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
    await asyncio.to_thread(get_user_config, message.chat.id)
    await message.answer(
        "👋 **Фитнес-хаб активен.**\n\n"
        "🥗 Присылай фото еды, надиктовывай голосом или пиши текстом.\n"
        "🏋️ Карточки и аналитика — в нижнем меню.\n"
        "🔄 Данные GymUp подтягиваются автоматически.",
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
    status_msg = await message.answer("🔬 Считаю последнюю сессию...")
    res = await asyncio.to_thread(get_training_analytics)
    await status_msg.edit_text(res, parse_mode="Markdown")


@dp.message(F.text == "⚙️ Настройки")
@dp.message(Command("settings"))
async def show_settings(message: types.Message):
    keyboard = await asyncio.to_thread(get_settings_keyboard, message.chat.id)
    await message.answer(
        "⚙️ **Управление уведомлениями:**",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


@dp.callback_query(F.data.startswith("toggle_"))
async def handle_toggle(cb: CallbackQuery):
    cfg = await asyncio.to_thread(get_user_config, cb.message.chat.id)
    if cb.data == "toggle_morning":
        await asyncio.to_thread(
            update_user_config, cb.message.chat.id, "morning_notify",
            not cfg.get("morning_notify", True)
        )
    elif cb.data == "toggle_casein":
        await asyncio.to_thread(
            update_user_config, cb.message.chat.id, "casein_notify",
            not cfg.get("casein_notify", True)
        )

    keyboard = await asyncio.to_thread(get_settings_keyboard, cb.message.chat.id)
    await cb.message.edit_reply_markup(reply_markup=keyboard)
    await cb.answer("Обновлено!")


@dp.callback_query(F.data.startswith("test_"))
async def handle_test_pushes(cb: CallbackQuery):
    if cb.data == "test_morning_push":
        await send_morning_split()
    elif cb.data == "test_casein_push":
        await send_casein_reminder()
    await cb.answer("Отправлено!")


# ----------------- ПИТАНИЕ: ТЕКСТ / ГОЛОС / ФОТО -----------------
MENU_BUTTONS = ("🏋️ Тренировка дня", "📊 Аналитика", "⚙️ Настройки")


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_food_text(message: types.Message):
    if message.text in MENU_BUTTONS:
        return

    status_msg = await message.answer("🔄 Анализирую состав...")
    try:
        prompt = (
            f"Рассчитай БЖУ продукта или блюда: '{message.text}'. "
            'Ответь только JSON: {"dish_name": "текст", "protein": число_грамм, "calories": число_ккал}'
        )
        data = await asyncio.to_thread(ask_gemini_json, [prompt])
        await log_and_reply(status_msg, data, message.text)
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

        prompt = (
            "Послушай аудиозапись, определи продукты и их граммовки. "
            'Ответь только JSON: {"dish_name": "название", "protein": число_грамм, "calories": число_ккал}'
        )
        parts = [
            genai_types.Part.from_bytes(data=voice_bytes, mime_type="audio/ogg"),
            prompt,
        ]
        data = await asyncio.to_thread(ask_gemini_json, parts)
        await log_and_reply(status_msg, data, "Голосовой ввод")
    except Exception:
        await status_msg.edit_text("⚠️ Не удалось распознать голос.")


@dp.message(F.photo)
async def handle_food_photo(message: types.Message):
    status_msg = await message.answer("🔍 Распознаю этикетку или блюдо...")
    try:
        photo_bytes = io.BytesIO()
        await bot.download(message.photo[-1], destination=photo_bytes)
        photo_bytes.seek(0)
        img = Image.open(photo_bytes)

        prompt = (
            "Ты спортивный нутрициолог. Распознай этикетку или блюдо на фото. "
            "Определи белки и калории на всю порцию или банку. "
            "Если вес порции не виден, оцени его и укажи оценку в dish_name. "
            'Ответь только JSON: {"dish_name": "название", "protein": число_грамм, "calories": число_ккал}'
        )
        data = await asyncio.to_thread(ask_gemini_json, [prompt, img])
        await log_and_reply(status_msg, data, "Блюдо по фото")
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