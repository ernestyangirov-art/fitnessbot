import asyncio
import html
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
BAR_WIDTH = 12
ANALYSIS_SHEET = "Разбор"
MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн",
          "июл", "авг", "сен", "окт", "ноя", "дек")


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


def _kg(value):
    return f"{value:,.0f}".replace(",", " ")


def _bar(value, top, width=BAR_WIDTH):
    if top <= 0:
        return "░" * width
    filled = min(width, round(width * value / top))
    return "▓" * filled + "░" * (width - filled)


def _short_date(key):
    try:
        d = datetime.strptime(key[:10], "%Y-%m-%d")
        return f"{d.day} {MONTHS[d.month - 1]}"
    except ValueError:
        return key[:10]


def _cut(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def group_sessions(rows):
    """Строки листа -> {дата_время: [строки]}"""
    sessions = {}
    for r in rows:
        if len(r) <= COL_HARD or not r[COL_DATE]:
            continue
        sessions.setdefault(r[COL_DATE], []).append(r)
    return sessions


def weekly_volume(sessions, last_key):
    """Подходы по паттернам за 7 дней до последней сессии включительно."""
    result = {}
    try:
        last_dt = datetime.strptime(last_key[:10], "%Y-%m-%d")
    except ValueError:
        return result
    for key, rws in sessions.items():
        try:
            day = datetime.strptime(key[:10], "%Y-%m-%d")
        except ValueError:
            continue
        if 0 <= (last_dt - day).days < 7:
            for r in rws:
                p = _pattern(r)
                if p != "Разминка":
                    result[p] = result.get(p, 0) + 1
    return result


def best_orm_before(sessions, order):
    """Лучший расчётный 1ПМ по каждому упражнению до последней сессии."""
    best = {}
    for key in order[:-1]:
        for r in sessions[key]:
            orm = _num(r[COL_ORM])
            if orm > best.get(r[COL_EXERCISE], 0):
                best[r[COL_EXERCISE]] = orm
    return best


def build_analytics(rows):
    """Блок с цифрами и шкалами. Без сетевых вызовов."""
    sessions = group_sessions(rows)
    if not sessions:
        return "Недостаточно данных для анализа."

    order = sorted(sessions)
    last_key = order[-1]
    last = sessions[last_key]
    split = last[0][COL_SPLIT] or "Тренировка"

    volume = sum(_num(r[COL_VOLUME]) for r in last)
    hard = sum(1 for r in last if _num(r[COL_HARD]) >= 4)
    total = len(last)

    prev_key, prev_volume = None, 0.0
    for key in reversed(order[:-1]):
        if (sessions[key][0][COL_SPLIT] or "Тренировка") == split:
            prev_key = key
            prev_volume = sum(_num(r[COL_VOLUME]) for r in sessions[key])
            break

    top = max(volume, prev_volume, 1)
    lines = ["ТОННАЖ ЖЕЛЕЗА",
             f"сейчас  {_bar(volume, top)}  {_kg(volume):>7} кг"]
    if prev_key:
        delta = volume - prev_volume
        pct = (delta / prev_volume * 100) if prev_volume else 0
        mark = "▲" if delta > 0 else ("▼" if delta < 0 else "=")
        lines.append(f"{_short_date(prev_key):<7} {_bar(prev_volume, top)}  {_kg(prev_volume):>7} кг")
        lines.append(f"{mark} {abs(pct):.0f} % к прошлой «{_cut(split, 16)}»")
    else:
        lines.append("прошлой сессии с этим сплитом нет")

    lines += ["", "ТЯЖЁЛЫЕ ПОДХОДЫ  (тяжесть 4-5)",
              f"{_bar(hard, total)}  {hard} из {total}  ({hard / total * 100:.0f} %)"]

    weekly = weekly_volume(sessions, last_key)
    if weekly:
        lines += ["", f"ОБЪЁМ ЗА 7 ДНЕЙ  (ориентир {WEEKLY_SET_LIMIT})"]
        width = max(len(k) for k in weekly)
        for name, count in sorted(weekly.items(), key=lambda x: -x[1]):
            flag = "  !" if count > WEEKLY_SET_LIMIT else ""
            lines.append(f"{name:<{width}}  {_bar(count, WEEKLY_SET_LIMIT)}  {count:>2}{flag}")

    before = best_orm_before(sessions, order)
    session_best = {}
    for r in last:
        orm = _num(r[COL_ORM])
        if orm > session_best.get(r[COL_EXERCISE], 0):
            session_best[r[COL_EXERCISE]] = orm

    if session_best:
        lines += ["", "РАСЧЁТНЫЙ 1ПМ  (★ личный рекорд)"]
        for name, orm in sorted(session_best.items(), key=lambda x: -x[1])[:6]:
            star = "★" if orm > before.get(name, 0) else " "
            lines.append(f"{star}{orm:>6.1f}  {_cut(name, 24)}")

        beaten = [(n, o, before.get(n, 0)) for n, o in session_best.items()
                  if o > before.get(n, 0)]
        if beaten:
            lines.append("")
            for name, now, was in sorted(beaten, key=lambda x: -(x[1] - x[2])):
                old = f"было {was:.1f}" if was else "впервые"
                lines.append(f"★ {_cut(name, 20)}: {now:.1f} ({old})")

    header = f"📊 <b>{html.escape(split.upper())}</b> · {_short_date(last_key)}"
    return f"{header}\n<pre>{html.escape(chr(10).join(lines))}</pre>"


def collect_facts(rows):
    """Фактический блок для модели. Возвращает (ключ_сессии, текст фактов)."""
    sessions = group_sessions(rows)
    if not sessions:
        return None, ""

    order = sorted(sessions)
    last_key = order[-1]
    last = sessions[last_key]
    split = last[0][COL_SPLIT] or "Тренировка"

    def describe(key):
        rws = sessions[key]
        vol = sum(_num(r[COL_VOLUME]) for r in rws)
        per = vol / len(rws) if rws else 0
        return f"  {key[:10]}: {len(rws)} подх., {vol:.0f} кг, {per:.0f} кг/подход"

    facts = [f"Сплит: {split}. Дата: {last_key[:10]}.",
             "История этого сплита (свежие сверху):"]
    same = [k for k in order if (sessions[k][0][COL_SPLIT] or "Тренировка") == split][-3:]
    facts += [describe(k) for k in reversed(same)]

    hard = sum(1 for r in last if _num(r[COL_HARD]) >= 4)
    avg = sum(_num(r[COL_HARD]) for r in last) / len(last)
    facts.append(f"Средняя тяжесть подхода: {avg:.2f} из 5.")
    facts.append(f"Подходов с тяжестью 4-5: {hard} из {len(last)}.")

    gaps = []
    recent = order[-7:]
    for i in range(len(recent) - 1):
        try:
            a = datetime.strptime(recent[i][:10], "%Y-%m-%d")
            b = datetime.strptime(recent[i + 1][:10], "%Y-%m-%d")
            gaps.append(str((b - a).days))
        except ValueError:
            continue
    if gaps:
        facts.append("Перерывы между последними тренировками, дней: " + ", ".join(gaps))

    weekly = weekly_volume(sessions, last_key)
    if weekly:
        facts.append("Подходов за 7 дней: " + ", ".join(
            f"{k} {v}" for k, v in sorted(weekly.items(), key=lambda x: -x[1])))

    before = best_orm_before(sessions, order)
    records = {}
    for r in last:
        orm = _num(r[COL_ORM])
        if orm > 0 and orm > before.get(r[COL_EXERCISE], 0):
            if orm > records.get(r[COL_EXERCISE], (0, 0))[0]:
                records[r[COL_EXERCISE]] = (orm, before.get(r[COL_EXERCISE], 0))
    if records:
        facts.append("Побитые рекорды 1ПМ: " + ", ".join(
            f"{n} {now:.1f} (было {was:.1f})" if was else f"{n} {now:.1f} (впервые)"
            for n, (now, was) in records.items()))
    else:
        facts.append("Побитых рекордов 1ПМ нет.")

    return last_key, "\n".join(facts)


ANALYSIS_PROMPT = """Ты тренер-методист с научным подходом к силовому тренингу.
Разбери тренировку по данным ниже.

СТРУКТУРА (ровно три абзаца):
1. Что произошло с нагрузкой — с опорой на конкретные числа.
2. Что это значит для прогресса.
3. Что делать на следующей такой тренировке — одно-два конкретных действия с числами.

ЖЁСТКИЕ ПРАВИЛА:
- Опирайся ТОЛЬКО на приведённые числа. Не выдумывай новых цифр, упражнений и дат.
- Не объясняй причины, которых нет в данных. Запрещено писать про утомление, восстановление, сон, стресс и корреляции между ними — этих данных у тебя нет.
- Критикуй только то, что видно в тренде из трёх и более сессий. Разовое отклонение просто констатируй.
- Отличай рост объёма (больше подходов) от роста интенсивности (больше кг на подход).
- Резкий скачок расчётного 1ПМ обычно означает смену снаряда или диапазона повторов, а не прирост силы.
- Пиши живо и коротко, как тренер в зале. Запрещены обороты: «показатель», «зафиксировано», «требует дальнейшего наблюдения», «наблюдается», «данные указывают».
- Без похвалы ради похвалы и без общих слов про пользу спорта.
- Обращайся на «ты». Обычный текст без разметки и заголовков.

ДАННЫЕ:
{facts}"""


def ask_gemini_text(prompt):
    if not gemini:
        raise RuntimeError("GEMINI_API_KEY не задан")
    resp = gemini.models.generate_content(model=GEMINI_MODEL, contents=[prompt])
    return (resp.text or "").strip()


def get_cached_analysis(session_key):
    ws = open_sheet(ANALYSIS_SHEET, ["Ключ сессии", "Разбор", "Сгенерирован"], cols=3)
    if not ws:
        return None
    try:
        for row in ws.get_all_values()[1:]:
            if row and row[0] == session_key and len(row) > 1 and row[1]:
                return row[1]
    except Exception:
        pass
    return None


def save_analysis(session_key, text):
    ws = open_sheet(ANALYSIS_SHEET, ["Ключ сессии", "Разбор", "Сгенерирован"], cols=3)
    if not ws:
        return
    try:
        ws.append_row([session_key, text, datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    except Exception:
        pass


def get_analysis(session_key, facts):
    """Разбор от модели. Считается один раз на сессию и кэшируется в таблице."""
    if not session_key:
        return "Нет данных для разбора."

    cached = get_cached_analysis(session_key)
    if cached:
        return f"🧠 <b>Разбор</b>\n\n{html.escape(cached)}"

    try:
        text = ask_gemini_text(ANALYSIS_PROMPT.format(facts=facts))
    except Exception:
        return "⚠️ Не удалось получить разбор. Цифры выше актуальны."

    if not text:
        return "⚠️ Модель вернула пустой ответ."

    save_analysis(session_key, text)
    return f"🧠 <b>Разбор</b>\n\n{html.escape(text)}"


def read_training_rows():
    ws = open_sheet(TRAINING_SHEET)
    if not ws:
        return None
    try:
        rows = ws.get_all_values()
    except Exception:
        return None
    return rows[1:] if len(rows) > 1 else []


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

    rows = await asyncio.to_thread(read_training_rows)
    if rows is None:
        await status_msg.edit_text("⚠️ Нет доступа к листу «Тренировки».")
        return
    if not rows:
        await status_msg.edit_text("Лист «Тренировки» пуст. Дождись синхронизации GymUp.")
        return

    numbers = await asyncio.to_thread(build_analytics, rows)
    await status_msg.edit_text(numbers, parse_mode="HTML")

    session_key, facts = await asyncio.to_thread(collect_facts, rows)
    if not session_key:
        return

    thinking = await message.answer("🧠 Готовлю разбор...")
    analysis = await asyncio.to_thread(get_analysis, session_key, facts)
    await thinking.edit_text(analysis, parse_mode="HTML")


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