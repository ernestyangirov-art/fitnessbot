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

DAILY_PROTEIN_TARGET = 150   # г
DAILY_CALORIE_TARGET = 2300  # ккал

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
    """Возвращает лист таблицы, создавая его при необходимости."""
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
SETTINGS_HEADERS = ["chat_id", "morning_notify", "casein_notify", "weight_notify"]
DEFAULT_CONFIG = {"morning_notify": True, "casein_notify": True, "weight_notify": True}

_settings_cache = None


def _parse_flag(value):
    return str(value).strip().upper() in ("TRUE", "1", "ДА", "YES")


def settings_sheet():
    return open_sheet(SETTINGS_SHEET, SETTINGS_HEADERS, cols=3)


def load_settings(force=False):
    global _settings_cache
    if _settings_cache is not None and not force:
        return _settings_cache

    ws = settings_sheet()
    result = {}
    if ws:
        try:
            for row in ws.get_all_values()[1:]:
                if row and row[0]:
                    result[str(row[0])] = {
                        "morning_notify": _parse_flag(row[1]) if len(row) > 1 else True,
                        "casein_notify": _parse_flag(row[2]) if len(row) > 2 else True,
                        "weight_notify": _parse_flag(row[3]) if len(row) > 3 else True,
                    }
        except Exception:
            pass
    _settings_cache = result
    return result


def _persist_user(cid, cfg):
    ws = settings_sheet()
    if not ws:
        return
    row = [cid, str(cfg["morning_notify"]).upper(), str(cfg["casein_notify"]).upper(),
           str(cfg["weight_notify"]).upper()]
    try:
        for idx, existing in enumerate(ws.get_all_values(), start=1):
            if existing and existing[0] == cid:
                ws.update(range_name=f"A{idx}:D{idx}", values=[row])
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