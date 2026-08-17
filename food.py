# -*- coding: utf-8 -*-
"""Дневник еды: запись приёмов пищи, правка, избранное, сводки."""

import html
from datetime import datetime

from core import (DAILY_CALORIE_TARGET, DAILY_PROTEIN_TARGET, FAV_SHEET,
                  FOOD_SHEET, col_letter, cut, header_index, num, open_sheet,
                  progress_bar)

FOOD_HEADERS = [
    "meal_id", "Дата и время", "Блюдо", "Вес порции (г)", "Ингредиенты",
    "Белки (г)", "Жиры (г)", "Углеводы (г)", "Клетчатка (г)", "Калории",
    "Источник", "Ручные поля",
]
FAV_HEADERS = ["fav_id", "Блюдо", "Вес порции (г)", "Ингредиенты",
               "Белки (г)", "Жиры (г)", "Углеводы (г)", "Клетчатка (г)",
               "Калории", "Использований"]

# Колонки листов «Питание» и «Избранное» — резолвятся по заголовку при
# каждом food_sheet()/fav_sheet() (оба листа бот создаёт и пишет сам, они
# всегда открываются раньше, чем M_*/F_* где-то читаются).
M_ID = M_DATE = M_DISH = M_WEIGHT = M_INGR = None
M_PROT = M_FAT = M_CARB = M_FIBER = M_KCAL = M_SRC = M_MANUAL = None
F_ID = F_DISH = F_WEIGHT = F_INGR = None
F_PROT = F_FAT = F_CARB = F_FIBER = F_KCAL = F_USED = None


def _resolve_food_columns(header_row):
    global M_ID, M_DATE, M_DISH, M_WEIGHT, M_INGR
    global M_PROT, M_FAT, M_CARB, M_FIBER, M_KCAL, M_SRC, M_MANUAL
    idx = header_index(header_row, FOOD_HEADERS, FOOD_SHEET)
    M_ID = idx["meal_id"]
    M_DATE = idx["Дата и время"]
    M_DISH = idx["Блюдо"]
    M_WEIGHT = idx["Вес порции (г)"]
    M_INGR = idx["Ингредиенты"]
    M_PROT = idx["Белки (г)"]
    M_FAT = idx["Жиры (г)"]
    M_CARB = idx["Углеводы (г)"]
    M_FIBER = idx["Клетчатка (г)"]
    M_KCAL = idx["Калории"]
    M_SRC = idx["Источник"]
    M_MANUAL = idx["Ручные поля"]


def _resolve_fav_columns(header_row):
    global F_ID, F_DISH, F_WEIGHT, F_INGR, F_PROT, F_FAT, F_CARB, F_FIBER, F_KCAL, F_USED
    idx = header_index(header_row, FAV_HEADERS, FAV_SHEET)
    F_ID = idx["fav_id"]
    F_DISH = idx["Блюдо"]
    F_WEIGHT = idx["Вес порции (г)"]
    F_INGR = idx["Ингредиенты"]
    F_PROT = idx["Белки (г)"]
    F_FAT = idx["Жиры (г)"]
    F_CARB = idx["Углеводы (г)"]
    F_FIBER = idx["Клетчатка (г)"]
    F_KCAL = idx["Калории"]
    F_USED = idx["Использований"]

# Сколько раз блюдо должно повториться, чтобы уехать в избранное само
FAV_AUTO_AFTER = 3

FOOD_JSON_SPEC = (
    'Ответь только JSON вида: {"dish_name": "название", "portion_g": вес_порции_в_граммах, '
    '"ingredients": [{"name": "продукт", "grams": вес}], "protein": г, "fat": г, '
    '"carbs": г, "fiber": г, "calories": ккал}. '
    "Все числа — на всю порцию, а не на 100 г. "
    "Ингредиенты перечисли максимально подробно, их граммовки в сумме должны давать portion_g."
)


# ----------------- ДОСТУП К ЛИСТАМ -----------------
def food_sheet():
    ws = open_sheet(FOOD_SHEET, FOOD_HEADERS, cols=len(FOOD_HEADERS))
    if ws:
        _resolve_food_columns(ws.row_values(1))
    return ws


def fav_sheet():
    ws = open_sheet(FAV_SHEET, FAV_HEADERS, cols=len(FAV_HEADERS))
    if ws:
        _resolve_fav_columns(ws.row_values(1))
    return ws


def new_meal_id():
    return "m" + datetime.now().strftime("%y%m%d%H%M%S%f")


def ingredients_text(data):
    """Список ингредиентов от модели -> строка для ячейки."""
    parts = []
    for item in data.get("ingredients") or []:
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            grams = num(item.get("grams", 0))
            if name:
                parts.append(f"{name} {grams:g} г" if grams else name)
        elif item:
            parts.append(str(item))
    return "; ".join(parts)


def meal_from_data(data, source):
    """JSON от модели -> строка листа «Питание»."""
    return [
        new_meal_id(),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        str(data.get("dish_name", "Без названия")),
        round(num(data.get("portion_g")), 1),
        ingredients_text(data),
        round(num(data.get("protein")), 1),
        round(num(data.get("fat")), 1),
        round(num(data.get("carbs")), 1),
        round(num(data.get("fiber")), 1),
        round(num(data.get("calories"))),
        source,
        "",  # Ручные поля — пусто, пока ничего не редактировали вручную
    ]


def append_meal(row):
    ws = food_sheet()
    if ws:
        ws.append_row(row, value_input_option="USER_ENTERED")
    return row


def read_meals():
    ws = food_sheet()
    if not ws:
        return []
    try:
        return [r for r in ws.get_all_values()[1:] if r and r[M_ID]]
    except Exception:
        return []


def today_meals(rows=None):
    rows = read_meals() if rows is None else rows
    today = datetime.now().strftime("%Y-%m-%d")
    return [r for r in rows if len(r) > M_DATE and r[M_DATE].startswith(today)]


def totals(rows):
    keys = ("protein", "fat", "carbs", "fiber", "kcal")
    cols = (M_PROT, M_FAT, M_CARB, M_FIBER, M_KCAL)
    return {k: sum(num(r[c]) for r in rows if len(r) > c) for k, c in zip(keys, cols)}


def find_meal(meal_id):
    """Возвращает (номер строки в листе, строка) либо (None, None)."""
    ws = food_sheet()
    if not ws:
        return None, None
    try:
        for idx, row in enumerate(ws.get_all_values(), start=1):
            if row and row[M_ID] == meal_id:
                return idx, row
    except Exception:
        pass
    return None, None


def _manual_fields(row):
    """Поля записи, отредактированные вручную (см. edit_meal_field) —
    их не трогает пропорциональный пересчёт по весу."""
    return set(f for f in str(row[M_MANUAL]).split(",") if f) if len(row) > M_MANUAL else set()


def rescale_meal(meal_id, new_weight):
    """Пересчитывает нутриенты пропорционально новому весу порции.

    Поля, отмеченные как отредактированные вручную (edit_meal_field),
    пропускает — иначе кнопки +50г/−50г затирали бы ручной ввод."""
    idx, row = find_meal(meal_id)
    if not idx:
        return None
    old = num(row[M_WEIGHT])
    if old <= 0 or new_weight <= 0:
        return None

    k = new_weight / old
    row = list(row) + [""] * (len(FOOD_HEADERS) - len(row))
    manual = _manual_fields(row)
    row[M_WEIGHT] = round(new_weight, 1)
    for col, key in ((M_PROT, "protein"), (M_FAT, "fat"),
                     (M_CARB, "carbs"), (M_FIBER, "fiber")):
        if key not in manual:
            row[col] = round(num(row[col]) * k, 1)
    if "calories" not in manual:
        row[M_KCAL] = round(num(row[M_KCAL]) * k)

    try:
        last = col_letter(len(FOOD_HEADERS))
        food_sheet().update(range_name=f"A{idx}:{last}{idx}",
                            values=[row[:len(FOOD_HEADERS)]])
    except Exception:
        return None
    return row


EDITABLE_FIELDS = {
    "dish": "Название",
    "protein": "Белки",
    "fat": "Жиры",
    "carbs": "Углеводы",
    "fiber": "Клетчатка",
    "calories": "Калории",
}


def edit_meal_field(meal_id, field_key, value):
    """Правит одно поле записи вручную и помечает его в «Ручные поля» —
    rescale_meal больше не будет его трогать при пересчёте по весу."""
    if field_key not in EDITABLE_FIELDS:
        return None
    idx, row = find_meal(meal_id)
    if not idx:
        return None
    row = list(row) + [""] * (len(FOOD_HEADERS) - len(row))

    col = {"dish": M_DISH, "protein": M_PROT, "fat": M_FAT,
           "carbs": M_CARB, "fiber": M_FIBER, "calories": M_KCAL}[field_key]
    if field_key == "dish":
        row[col] = str(value).strip()
    elif field_key == "calories":
        row[col] = round(num(value))
    else:
        row[col] = round(num(value), 1)

    manual = _manual_fields(row)
    manual.add(field_key)
    row[M_MANUAL] = ",".join(sorted(manual))

    try:
        last = col_letter(len(FOOD_HEADERS))
        food_sheet().update(range_name=f"A{idx}:{last}{idx}",
                            values=[row[:len(FOOD_HEADERS)]])
    except Exception:
        return None
    return row


def delete_meal(meal_id):
    idx, _ = find_meal(meal_id)
    if not idx:
        return False
    try:
        food_sheet().delete_rows(idx)
        return True
    except Exception:
        return False


# ----------------- ИЗБРАННОЕ -----------------
def read_favourites():
    ws = fav_sheet()
    if not ws:
        return []
    try:
        return [r for r in ws.get_all_values()[1:] if r and r[F_ID]]
    except Exception:
        return []


def add_favourite(row):
    """Кладёт блюдо в избранное. Повтор по названию не создаёт дубль."""
    ws = fav_sheet()
    if not ws:
        return None
    dish = row[M_DISH]
    try:
        for existing in ws.get_all_values()[1:]:
            if len(existing) > F_DISH and existing[F_DISH] == dish:
                return existing[F_ID]
        fav_id = "f" + datetime.now().strftime("%y%m%d%H%M%S%f")
        ws.append_row([fav_id, dish, row[M_WEIGHT], row[M_INGR], row[M_PROT],
                       row[M_FAT], row[M_CARB], row[M_FIBER], row[M_KCAL], 1],
                      value_input_option="USER_ENTERED")
        return fav_id
    except Exception:
        return None


def maybe_autofavourite(dish, rows):
    """Блюдо, встретившееся FAV_AUTO_AFTER раз, уезжает в избранное само."""
    count = sum(1 for r in rows if len(r) > M_DISH and r[M_DISH] == dish)
    if count < FAV_AUTO_AFTER:
        return False
    if any(len(f) > F_DISH and f[F_DISH] == dish for f in read_favourites()):
        return False
    for r in reversed(rows):
        if len(r) > M_DISH and r[M_DISH] == dish:
            return bool(add_favourite(r))
    return False


def meal_from_favourite(fav_id):
    for f in read_favourites():
        if f[F_ID] == fav_id:
            return [
                new_meal_id(),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                f[F_DISH], num(f[F_WEIGHT]),
                f[F_INGR] if len(f) > F_INGR else "",
                num(f[F_PROT]), num(f[F_FAT]), num(f[F_CARB]),
                num(f[F_FIBER]), num(f[F_KCAL]),
                "избранное",
                "",  # Ручные поля
            ]
    return None


def bump_favourite(fav_id):
    """Счётчик использований избранного блюда."""
    ws = fav_sheet()
    if not ws:
        return
    try:
        for idx, row in enumerate(ws.get_all_values(), start=1):
            if row and len(row) > F_ID and row[F_ID] == fav_id:
                used = num(row[F_USED]) if len(row) > F_USED else 0
                ws.update_cell(idx, F_USED + 1, int(used) + 1)
                return
    except Exception:
        pass


# ----------------- ОФОРМЛЕНИЕ -----------------
def _targets(targets):
    """Цели по умолчанию, если вызывающий не передал персональные (per-user,
    из «Настройки»)."""
    return targets or {"protein": DAILY_PROTEIN_TARGET, "calories": DAILY_CALORIE_TARGET}


def format_meal_card(row, day, targets=None):
    """Карточка только что записанного приёма пищи."""
    tgt = _targets(targets)
    weight = num(row[M_WEIGHT])
    head = html.escape(str(row[M_DISH]))
    if weight:
        head += f" · {weight:g} г"

    block = [
        f"Белки     {num(row[M_PROT]):>6.1f} г",
        f"Жиры      {num(row[M_FAT]):>6.1f} г",
        f"Углеводы  {num(row[M_CARB]):>6.1f} г",
        f"Клетчатка {num(row[M_FIBER]):>6.1f} г",
        f"Калории   {num(row[M_KCAL]):>6.0f}",
        "",
        "ЗА СЕГОДНЯ",
        f"Белок    {day['protein']:.0f} / {tgt['protein']:g} г",
        progress_bar(day["protein"], tgt["protein"]),
        f"Калории  {day['kcal']:.0f} / {tgt['calories']:g}",
        progress_bar(day["kcal"], tgt["calories"]),
    ]
    ingr = row[M_INGR]
    tail = f"\n<i>{html.escape(cut(str(ingr), 200))}</i>" if ingr else ""
    return f"✅ <b>{head}</b>\n<pre>{html.escape(chr(10).join(block))}</pre>{tail}"


def format_day(rows, targets=None):
    if not rows:
        return "🍽 <b>Сегодня</b>\n\nЗаписей пока нет."

    tgt = _targets(targets)
    day = totals(rows)
    lines = []
    for r in rows:
        weight = num(r[M_WEIGHT])
        mark = f" {weight:g}г" if weight else ""
        lines.append(f"{r[M_DATE][11:16]}  {cut(str(r[M_DISH]), 22)}{mark}")
        lines.append(f"        Б {num(r[M_PROT]):.0f}  К {num(r[M_KCAL]):.0f}")

    lines += ["", "ИТОГО",
              f"Белок    {day['protein']:.0f} / {tgt['protein']:g} г",
              progress_bar(day["protein"], tgt["protein"]),
              f"Калории  {day['kcal']:.0f} / {tgt['calories']:g}",
              progress_bar(day["kcal"], tgt["calories"]),
              f"Ж {day['fat']:.0f} г · У {day['carbs']:.0f} г · "
              f"Клетчатка {day['fiber']:.0f} г"]
    return f"🍽 <b>Сегодня</b>\n<pre>{html.escape(chr(10).join(lines))}</pre>"


def format_week(rows, targets=None):
    """Сводка за 7 дней: средние за день и сколько дней цель закрыта."""
    tgt = _targets(targets)
    by_day = {}
    for r in rows:
        if len(r) > M_KCAL and r[M_DATE]:
            by_day.setdefault(r[M_DATE][:10], []).append(r)
    if not by_day:
        return "📅 <b>Неделя</b>\n\nДанных пока нет."

    days = sorted(by_day)[-7:]
    lines, prot_ok, kcal_sum, prot_sum = [], 0, 0.0, 0.0
    for d in days:
        day_totals = totals(by_day[d])
        prot_sum += day_totals["protein"]
        kcal_sum += day_totals["kcal"]
        if day_totals["protein"] >= tgt["protein"]:
            prot_ok += 1
        lines.append(f"{d[8:10]}.{d[5:7]}  Б {day_totals['protein']:>5.0f}  К {day_totals['kcal']:>5.0f}")

    n = len(days)
    lines += ["", f"Среднее за день:  Б {prot_sum / n:.0f} г · К {kcal_sum / n:.0f}",
              f"Цель по белку закрыта: {prot_ok} из {n} дн."]
    return f"📅 <b>Неделя</b>\n<pre>{html.escape(chr(10).join(lines))}</pre>"


def format_favourites(favs):
    if not favs:
        return ("⭐ <b>Избранное</b>\n\nПусто. Блюдо попадёт сюда по кнопке "
                f"или после {FAV_AUTO_AFTER} повторов.")
    lines = [f"{cut(str(f[F_DISH]), 26)}  Б {num(f[F_PROT]):.0f}  К {num(f[F_KCAL]):.0f}"
             for f in favs]
    return f"⭐ <b>Избранное</b>\n<pre>{html.escape(chr(10).join(lines))}</pre>"