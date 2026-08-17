# -*- coding: utf-8 -*-
"""Общий слой: конфигурация, Google Таблицы, клиент Gemini, утилиты."""

import json
import os

import gspread
from dotenv import load_dotenv
from google import genai
from google.oauth2 import service_account

load_dotenv()

# ----------------- КОНФИГУРАЦИЯ -----------------
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

DAILY_PROTEIN_TARGET = 150   # г — дефолт, если у пользователя нет своей цели
DAILY_CALORIE_TARGET = 2300  # ккал — дефолт, если у пользователя нет своей цели

# Границы разумного для ручного ввода цели — защита от опечаток.
PROTEIN_TARGET_RANGE = (30, 400)
CALORIE_TARGET_RANGE = (800, 6000)

TRAINING_SHEET = "Тренировки"
SETTINGS_SHEET = "Настройки"
ANALYSIS_SHEET = "Разбор"
FOOD_SHEET = "Питание"
FAV_SHEET = "Избранное"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан!")
if not SPREADSHEET_ID:
    raise ValueError("SPREADSHEET_ID не задан!")

gemini = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# ----------------- УТИЛИТЫ -----------------
def col_letter(n):
    """1-based номер колонки -> буква(ы) A1-нотации (1 -> A, 27 -> AA)."""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def header_index(header_row, required, sheet_name=""):
    """{название колонки: 0-based индекс} из строки заголовков листа.

    Падает, если какая-то из required колонок отсутствует — молча читать
    не ту колонку хуже, чем упасть с понятной ошибкой."""
    idx = {name: i for i, name in enumerate(header_row)}
    missing = [name for name in required if name not in idx]
    if missing:
        raise RuntimeError(f"Лист «{sheet_name}»: не найдены колонки {missing}")
    return idx


def num(value):
    """Мягкое приведение к числу: пустая или битая ячейка даёт 0."""
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return 0.0


def cut(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def bar(value, top, width=12):
    """Псевдографическая шкала."""
    if top <= 0:
        return "░" * width
    filled = min(width, round(width * value / top))
    return "▓" * filled + "░" * (width - filled)


def progress_bar(current, target, length=12):
    ratio = min(1.0, max(0.0, current / target)) if target > 0 else 0
    filled = int(ratio * length)
    return f"{'▓' * filled}{'░' * (length - filled)} {int(ratio * 100)} %"


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
    """Возвращает лист таблицы, создавая его при необходимости.

    Если передан headers — это лист, которым бот владеет целиком (Настройки,
    Питание, Избранное): бот вправе дописывать в него новые колонки, если
    код завёл новую константу, а в самой таблице её ещё нет (иначе схема
    в коде и в реальном листе расходятся молча). Листы без headers
    (Тренировки — пишет sync_gymup.py) не трогаются никогда."""
    creds = get_gcp_creds()
    if not creds or not SPREADSHEET_ID:
        return None
    book = gspread.authorize(creds).open_by_key(SPREADSHEET_ID)
    try:
        ws = book.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = book.add_worksheet(title=title, rows=1000, cols=cols)
        if headers:
            ws.append_row(headers)
        return ws

    if headers:
        try:
            current = ws.row_values(1)
            missing = [h for h in headers if h not in current]
            if missing:
                start = len(current) + 1
                ws.update(range_name=f"{col_letter(start)}1:{col_letter(start + len(missing) - 1)}1",
                         values=[missing])
        except Exception:
            pass
    return ws


def ask_gemini_text(prompt):
    """Свободный текстовый ответ модели."""
    if not gemini:
        raise RuntimeError("GEMINI_API_KEY не задан")
    resp = gemini.models.generate_content(model=GEMINI_MODEL, contents=[prompt])
    return (resp.text or "").strip()


def ask_gemini_json(parts):
    """Строгий JSON-ответ модели."""
    if not gemini:
        raise RuntimeError("GEMINI_API_KEY не задан")
    from google.genai import types as genai_types
    resp = gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=parts,
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json"
        ),
    )
    text = (resp.text or "").strip().replace("```json", "").replace("```", "")
    return json.loads(text)


# ----------------- НАСТРОЙКИ ПОЛЬЗОВАТЕЛЯ -----------------
# Диск на Render эфемерный, поэтому настройки живут в таблице.
SETTINGS_HEADERS = ["chat_id", "morning_notify", "casein_notify", "weight_notify",
                     "daily_protein_target", "daily_calorie_target"]
DEFAULT_CONFIG = {
    "morning_notify": True, "casein_notify": True, "weight_notify": True,
    "daily_protein_target": DAILY_PROTEIN_TARGET,
    "daily_calorie_target": DAILY_CALORIE_TARGET,
}

_settings_cache = None

# Колонки листа «Настройки» — резолвятся по заголовку при каждом обращении
# к листу, не хардкодятся индексом (settings_sheet() всегда вызывается
# первой, поэтому к моменту любого использования S_* уже заполнены).
S_CHAT = S_MORNING = S_CASEIN = S_WEIGHT = S_PROTEIN = S_CALORIES = None


def _resolve_settings_columns(header_row):
    global S_CHAT, S_MORNING, S_CASEIN, S_WEIGHT, S_PROTEIN, S_CALORIES
    idx = header_index(header_row, SETTINGS_HEADERS, SETTINGS_SHEET)
    S_CHAT = idx["chat_id"]
    S_MORNING = idx["morning_notify"]
    S_CASEIN = idx["casein_notify"]
    S_WEIGHT = idx["weight_notify"]
    S_PROTEIN = idx["daily_protein_target"]
    S_CALORIES = idx["daily_calorie_target"]


def _parse_flag(value):
    return str(value).strip().upper() in ("TRUE", "1", "ДА", "YES")


def _parse_num(value, default):
    v = num(value)
    return v if v > 0 else default


def settings_sheet():
    ws = open_sheet(SETTINGS_SHEET, SETTINGS_HEADERS, cols=len(SETTINGS_HEADERS))
    if ws:
        _resolve_settings_columns(ws.row_values(1))
    return ws


def load_settings(force=False):
    global _settings_cache
    if _settings_cache is not None and not force:
        return _settings_cache

    ws = settings_sheet()
    result = {}
    if ws:
        try:
            for row in ws.get_all_values()[1:]:
                if row and len(row) > S_CHAT and row[S_CHAT]:
                    result[str(row[S_CHAT])] = {
                        "morning_notify": _parse_flag(row[S_MORNING]) if len(row) > S_MORNING else True,
                        "casein_notify": _parse_flag(row[S_CASEIN]) if len(row) > S_CASEIN else True,
                        "weight_notify": _parse_flag(row[S_WEIGHT]) if len(row) > S_WEIGHT else True,
                        "daily_protein_target": _parse_num(row[S_PROTEIN], DAILY_PROTEIN_TARGET)
                            if len(row) > S_PROTEIN else DAILY_PROTEIN_TARGET,
                        "daily_calorie_target": _parse_num(row[S_CALORIES], DAILY_CALORIE_TARGET)
                            if len(row) > S_CALORIES else DAILY_CALORIE_TARGET,
                    }
        except Exception:
            pass
    _settings_cache = result
    return result


def _persist_user(cid, cfg):
    ws = settings_sheet()
    if not ws:
        return
    try:
        row = [""] * len(SETTINGS_HEADERS)
        row[S_CHAT] = cid
        row[S_MORNING] = str(cfg["morning_notify"]).upper()
        row[S_CASEIN] = str(cfg["casein_notify"]).upper()
        row[S_WEIGHT] = str(cfg["weight_notify"]).upper()
        row[S_PROTEIN] = cfg["daily_protein_target"]
        row[S_CALORIES] = cfg["daily_calorie_target"]

        for idx, existing in enumerate(ws.get_all_values(), start=1):
            if existing and len(existing) > S_CHAT and existing[S_CHAT] == cid:
                last = col_letter(len(SETTINGS_HEADERS))
                ws.update(range_name=f"A{idx}:{last}{idx}", values=[row])
                return
        ws.append_row(row)
    except Exception:
        pass


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