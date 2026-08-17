# -*- coding: utf-8 -*-
"""Вес тела: запись, история, подстановка в расчёты."""

import html
from datetime import datetime

from core import bar, num, open_sheet

WEIGHT_SHEET = "Вес"
WEIGHT_HEADERS = ["Дата", "Вес (кг)"]

W_DATE, W_KG = 0, 1

# Границы разумного: защита от опечатки вроде 8.5 или 850
MIN_WEIGHT, MAX_WEIGHT = 30.0, 300.0


def weight_sheet():
    return open_sheet(WEIGHT_SHEET, WEIGHT_HEADERS, cols=2)


def read_weights():
    """Все замеры, отсортированные по дате. [(дата, кг), ...]"""
    ws = weight_sheet()
    if not ws:
        return []
    out = []
    try:
        for row in ws.get_all_values()[1:]:
            if row and row[W_DATE] and len(row) > W_KG:
                kg = num(row[W_KG])
                if kg > 0:
                    out.append((row[W_DATE][:10], kg))
    except Exception:
        return []
    return sorted(out)


def save_weight(kg, day=None):
    """Записывает вес. Повторный ввод за ту же дату перезаписывает строку."""
    if not (MIN_WEIGHT <= kg <= MAX_WEIGHT):
        return False
    ws = weight_sheet()
    if not ws:
        return False

    day = day or datetime.now().strftime("%Y-%m-%d")
    try:
        for idx, row in enumerate(ws.get_all_values(), start=1):
            if row and row[W_DATE][:10] == day:
                ws.update(range_name=f"A{idx}:B{idx}", values=[[day, kg]])
                return True
        ws.append_row([day, kg], value_input_option="USER_ENTERED")
        return True
    except Exception:
        return False


def latest_weight(weights=None):
    weights = read_weights() if weights is None else weights
    return weights[-1][1] if weights else None


def weight_for_date(day, weights=None, fallback=None):
    """Вес, ближайший по времени к указанной дате.

    Нужен для исторических расчётов: подтягивания годовой давности
    нельзя пересчитывать по сегодняшнему весу.
    """
    weights = read_weights() if weights is None else weights
    if not weights:
        return fallback
    try:
        target = datetime.strptime(day[:10], "%Y-%m-%d")
    except ValueError:
        return weights[-1][1]

    best, best_gap = weights[-1][1], None
    for d, kg in weights:
        try:
            gap = abs((datetime.strptime(d, "%Y-%m-%d") - target).days)
        except ValueError:
            continue
        if best_gap is None or gap < best_gap:
            best, best_gap = kg, gap
    return best


def weight_trend(weights=None, days=28):
    """Изменение веса за период. Возвращает (первый, последний, дельта) либо None."""
    weights = read_weights() if weights is None else weights
    if len(weights) < 2:
        return None
    try:
        last_day = datetime.strptime(weights[-1][0], "%Y-%m-%d")
    except ValueError:
        return None

    window = []
    for d, kg in weights:
        try:
            if 0 <= (last_day - datetime.strptime(d, "%Y-%m-%d")).days <= days:
                window.append((d, kg))
        except ValueError:
            continue
    if len(window) < 2:
        return None
    return window[0][1], window[-1][1], window[-1][1] - window[0][1]


def format_weight(weights=None):
    weights = read_weights() if weights is None else weights
    if not weights:
        return ("⚖️ <b>Вес тела</b>\n\nЗаписей пока нет. "
                "Отправь /weight и число, например 84.5")

    recent = weights[-8:]
    low = min(kg for _, kg in recent)
    high = max(kg for _, kg in recent)
    span = max(high - low, 0.1)

    lines = []
    for d, kg in recent:
        lines.append(f"{d[8:10]}.{d[5:7]}  {kg:>5.1f}  {bar(kg - low, span, 10)}")

    trend = weight_trend(weights)
    if trend:
        first, last, delta = trend
        sign = "+" if delta > 0 else ""
        lines += ["", f"За 4 недели: {first:.1f} → {last:.1f} ({sign}{delta:.1f} кг)"]

    return f"⚖️ <b>Вес тела</b>\n<pre>{html.escape(chr(10).join(lines))}</pre>"