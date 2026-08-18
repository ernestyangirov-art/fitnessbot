# -*- coding: utf-8 -*-
"""Аналитика тренировок: цифры со шкалами и разбор от модели."""

import html
from datetime import datetime

from core import (ANALYSIS_SHEET, TRAINING_SHEET, ask_gemini_text, bar, cut,
                  header_index, num, open_sheet)

# Колонки листа «Тренировки» — резолвятся по заголовку в read_training_rows(),
# не хардкодятся индексом. Лист пишет sync_gymup.py (gymup-sync), бот его
# никогда не создаёт и не правит — только требует ровно эти заголовки,
# перестановка/пропажа колонки там падает явной ошибкой, а не читает не то.
TRAINING_HEADERS = [
    "Дата и время", "Программа", "Сплит", "Упражнение", "ex_id", "Вес (кг)", "Повторы",
    "Тоннаж внешний (кг)", "Расчётный 1ПМ (кг)", "Тяжесть (1-5)",
]

COL_DATE = COL_PROGRAM = COL_SPLIT = COL_EXERCISE = COL_EX_ID = None
COL_WEIGHT = COL_REPS = None
COL_VOLUME = COL_ORM = COL_HARD = None


def _resolve_training_columns(header_row):
    global COL_DATE, COL_PROGRAM, COL_SPLIT, COL_EXERCISE, COL_EX_ID
    global COL_WEIGHT, COL_REPS, COL_VOLUME, COL_ORM, COL_HARD
    idx = header_index(header_row, TRAINING_HEADERS, TRAINING_SHEET)
    COL_DATE = idx["Дата и время"]
    COL_PROGRAM = idx["Программа"]
    COL_SPLIT = idx["Сплит"]
    COL_EXERCISE = idx["Упражнение"]
    COL_EX_ID = idx["ex_id"]
    COL_WEIGHT = idx["Вес (кг)"]
    COL_REPS = idx["Повторы"]
    COL_VOLUME = idx["Тоннаж внешний (кг)"]
    COL_ORM = idx["Расчётный 1ПМ (кг)"]
    COL_HARD = idx["Тяжесть (1-5)"]

# Паттерн движения по id упражнения (коды мышц GymUp + ручные правки).
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

# Недельный ориентир подходов на паттерн (MEV/MRV, а не жёсткое правило).
WEEKLY_SET_LIMIT = 20
MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн",
          "июл", "авг", "сен", "окт", "ноя", "дек")


def kg(value):
    return f"{value:,.0f}".replace(",", " ")


def short_date(key):
    try:
        d = datetime.strptime(key[:10], "%Y-%m-%d")
        return f"{d.day} {MONTHS[d.month - 1]}"
    except ValueError:
        return key[:10]


def pattern_of(row):
    try:
        return EXERCISE_PATTERNS.get(int(num(row[COL_EX_ID])), "Прочее")
    except (IndexError, ValueError):
        return "Прочее"


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
                p = pattern_of(r)
                if p != "Разминка":
                    result[p] = result.get(p, 0) + 1
    return result


def best_orm_before(sessions, order):
    """Лучший расчётный 1ПМ по каждому упражнению до последней сессии."""
    best = {}
    for key in order[:-1]:
        for r in sessions[key]:
            orm = num(r[COL_ORM])
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

    volume = sum(num(r[COL_VOLUME]) for r in last)
    hard = sum(1 for r in last if num(r[COL_HARD]) >= 4)
    total = len(last)

    prev_key, prev_volume = None, 0.0
    for key in reversed(order[:-1]):
        if (sessions[key][0][COL_SPLIT] or "Тренировка") == split:
            prev_key = key
            prev_volume = sum(num(r[COL_VOLUME]) for r in sessions[key])
            break

    top = max(volume, prev_volume, 1)
    lines = ["ТОННАЖ ЖЕЛЕЗА",
             f"сейчас  {bar(volume, top)}  {kg(volume):>7} кг"]
    if prev_key:
        delta = volume - prev_volume
        pct = (delta / prev_volume * 100) if prev_volume else 0
        mark = "▲" if delta > 0 else ("▼" if delta < 0 else "=")
        lines.append(f"{short_date(prev_key):<7} {bar(prev_volume, top)}  {kg(prev_volume):>7} кг")
        lines.append(f"{mark} {abs(pct):.0f} % к прошлой «{cut(split, 16)}»")
    else:
        lines.append("прошлой сессии с этим сплитом нет")

    lines += ["", "ТЯЖЁЛЫЕ ПОДХОДЫ  (тяжесть 4-5)",
              f"{bar(hard, total)}  {hard} из {total}  ({hard / total * 100:.0f} %)"]

    weekly = weekly_volume(sessions, last_key)
    if weekly:
        lines += ["", f"ОБЪЁМ ЗА 7 ДНЕЙ  (ориентир {WEEKLY_SET_LIMIT})"]
        width = max(len(k) for k in weekly)
        for name, count in sorted(weekly.items(), key=lambda x: -x[1]):
            flag = "  !" if count > WEEKLY_SET_LIMIT else ""
            lines.append(f"{name:<{width}}  {bar(count, WEEKLY_SET_LIMIT)}  {count:>2}{flag}")

    before = best_orm_before(sessions, order)
    session_best = {}
    for r in last:
        orm = num(r[COL_ORM])
        if orm > session_best.get(r[COL_EXERCISE], 0):
            session_best[r[COL_EXERCISE]] = orm

    if session_best:
        lines += ["", "РАСЧЁТНЫЙ 1ПМ  (★ личный рекорд)"]
        for name, orm in sorted(session_best.items(), key=lambda x: -x[1])[:6]:
            star = "★" if orm > before.get(name, 0) else " "
            lines.append(f"{star}{orm:>6.1f}  {cut(name, 24)}")

        beaten = [(n, o, before.get(n, 0)) for n, o in session_best.items()
                  if o > before.get(n, 0)]
        if beaten:
            lines.append("")
            for name, now, was in sorted(beaten, key=lambda x: -(x[1] - x[2])):
                old = f"было {was:.1f}" if was else "впервые"
                lines.append(f"★ {cut(name, 20)}: {now:.1f} ({old})")

    header = f"📊 <b>{html.escape(split.upper())}</b> · {short_date(last_key)}"
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
        vol = sum(num(r[COL_VOLUME]) for r in rws)
        per = vol / len(rws) if rws else 0
        return f"  {key[:10]}: {len(rws)} подх., {vol:.0f} кг, {per:.0f} кг/подход"

    def top_exercises(key, top=3):
        """Ключевые упражнения сессии в формате вес×повторы."""
        by_name = {}
        for r in sessions[key]:
            by_name.setdefault(r[COL_EXERCISE], []).append(r)
        ranked = sorted(
            by_name.items(),
            key=lambda kv: -sum(num(r[COL_VOLUME]) for r in kv[1])
        )[:top]
        out = []
        for name, sets in ranked:
            parts = []
            for r in sets:
                w = num(r[COL_WEIGHT])
                reps = int(num(r[COL_REPS]))
                parts.append(f"{w:g}×{reps}" if w > 0 else f"св.вес×{reps}")
            out.append(f"  {name}: " + ", ".join(parts))
        return out

    facts = [f"Сплит: {split}. Дата: {last_key[:10]}.",
             "История этого сплита (свежие сверху):"]
    same = [k for k in order if (sessions[k][0][COL_SPLIT] or "Тренировка") == split][-3:]
    facts += [describe(k) for k in reversed(same)]

    facts.append("Ключевые упражнения этой сессии (вес×повторы):")
    facts += top_exercises(last_key)
    if len(same) > 1:
        facts.append(f"Ключевые упражнения {same[-2][:10]}:")
        facts += top_exercises(same[-2])

    hard = sum(1 for r in last if num(r[COL_HARD]) >= 4)
    avg = sum(num(r[COL_HARD]) for r in last) / len(last)
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
        orm = num(r[COL_ORM])
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
3. Что делать на следующей такой тренировке — одно-два конкретных действия.

ЖЁСТКИЕ ПРАВИЛА:
- Опирайся ТОЛЬКО на приведённые числа. Не выдумывай новых цифр, упражнений и дат.
- Не объясняй причины, которых нет в данных. Запрещено писать про утомление, восстановление, сон, стресс и корреляции между ними — этих данных у тебя нет.
- Рекомендации давай ТОЛЬКО в терминах конкретного упражнения и его подходов: какой вес и сколько повторов сделать в следующий раз. Пример правильной формулировки: «в тяге штанги стоит 70x6 три сессии подряд — попробуй 72.5x6».
- ЗАПРЕЩЕНО советовать менять средние и производные величины: средний вес на подход, среднюю тяжесть, общий тоннаж. Ими нельзя управлять напрямую, они складываются из состава тренировки.
- Тяжесть подхода ставится после выполнения, по ощущению. Не советуй планировать её заранее.
- Первый подход упражнения с заметно меньшим весом — это разминка, а не падение результата.
- Критикуй только то, что видно в тренде из трёх и более сессий. Разовое отклонение просто констатируй.
- Отличай рост объёма (больше подходов) от роста интенсивности (больше кг на подход).
- Резкий скачок расчётного 1ПМ обычно означает смену снаряда или диапазона повторов, а не прирост силы.
- Пиши живо и коротко, как тренер в зале. Запрещены обороты: «показатель», «зафиксировано», «требует дальнейшего наблюдения», «наблюдается», «данные указывают».
- Без похвалы ради похвалы и без общих слов про пользу спорта.
- Обращайся на «ты». Обычный текст без разметки и заголовков.

ДАННЫЕ:
{facts}"""

ANALYSIS_HEADERS = ["Ключ сессии", "Разбор", "Сгенерирован"]
A_KEY = A_TEXT = A_GEN = None


def _resolve_analysis_columns(header_row):
    global A_KEY, A_TEXT, A_GEN
    idx = header_index(header_row, ANALYSIS_HEADERS, ANALYSIS_SHEET)
    A_KEY = idx["Ключ сессии"]
    A_TEXT = idx["Разбор"]
    A_GEN = idx["Сгенерирован"]


def analysis_sheet():
    ws = open_sheet(ANALYSIS_SHEET, ANALYSIS_HEADERS, cols=len(ANALYSIS_HEADERS))
    if ws:
        _resolve_analysis_columns(ws.row_values(1))
    return ws


def get_cached_analysis(session_key):
    ws = analysis_sheet()
    if not ws:
        return None
    try:
        for row in ws.get_all_values()[1:]:
            if row and row[A_KEY] == session_key and len(row) > A_TEXT and row[A_TEXT]:
                return row[A_TEXT]
    except Exception:
        pass
    return None


def save_analysis(session_key, text):
    ws = analysis_sheet()
    if not ws:
        return
    try:
        ws.append_row([session_key, text,
                       datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
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
    if not rows:
        return []
    _resolve_training_columns(rows[0])
    return rows[1:] if len(rows) > 1 else []