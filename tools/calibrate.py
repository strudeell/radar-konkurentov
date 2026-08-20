#!/usr/bin/env python3
"""Калибровка на живых данных — Фаза 6.

Зачем эта программа. Радар уже умеет собирать, сравнивать, решать, что срочно,
и отправлять. Чего он не умеет — сказать, был ли он прав. Порог в 120 символов,
словарь шумодава и список из шестидесяти источников выбраны по одному дню
наблюдений и по здравому смыслу. Неделя обкатки существует ровно затем, чтобы
заменить здравый смысл измерением, а эта программа — затем, чтобы измерение
было по чему сделать.

Как это устроено. Программа ничего не решает сама — она готовит решение
человеку, в три шага:

1. Собирает всё, что радар нашёл за неделю, тем же разбором, каким он работает
   в бою: те же дельты, тот же классификатор, тот же порог. Не копия логики, а
   она сама — иначе калибровался бы не радар, а его пересказ.
2. Заводит лист разметки. Против каждой находки человек ставит одно слово:
   «сигнал» или «шум». Это единственная ручная работа фазы — минут пять в день.
3. По размеченному считает то, ради чего фаза и придумана: сколько шума дошло бы
   до человека при разных порогах, какие строки шумят изо дня в день (кандидаты
   в шумодав) и какие источники за неделю не дали ни одного сигнала.

Почему разметка руками. Отличить «конкурент поменял цену» от «на странице
повернулась карусель отзывов» может только тот, кому эти сведения нужны.
Автоматическая разметка означала бы, что радар сам себе ставит оценку — и
поставит её так, чтобы сойтись с тем, что он уже умеет.

Ноль — это тоже измерение. Если находок за неделю не было вовсе, это не «нет
данных», а результат: у конкурентов правда тихо, и порог менять не на чем.
Программа такие случаи называет вслух, а не показывает пустую таблицу.

Запуск:

    python tools/calibrate.py                отчёт по накопленному
    python tools/calibrate.py --razmetka     завести или дополнить лист разметки
    python tools/calibrate.py --days 14      взять другой срок
    python tools/calibrate.py --to 2026-08-27  считать неделю до этого дня
    python tools/calibrate.py --lines        показать строки находок целиком
    python tools/calibrate.py --dry-run      ничего не записывать
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import yaml  # noqa: E402

import classify  # noqa: E402
import console  # noqa: E402
import diffing  # noqa: E402
import wording  # noqa: E402

import notify  # noqa: E402  — тот же разбор, которым работает боевой радар

CALIB = ROOT / "calibration"
SHEET = CALIB / "razmetka.yaml"
RUNS = ROOT / "runs"

SIGNAL = "сигнал"
NOISE = "шум"
UNMARKED = "?"
VERDICTS = (SIGNAL, NOISE)

# Пороги, которые примеряем. Ниже 60 опускаться незачем: фоновый шум одной
# страницы, замеренный tools/noise_check.py, живёт в этих пределах. Выше 800
# начинается «новая статья в блоге — не изменение», а это уже не калибровка.
LADDER = (0, 60, 90, 120, 180, 250, 400, 800)

# Сколько кандидатов в шумодав показывать. Больше — список перестают читать.
CANDIDATES = 15

# Планка, ниже которой советовать порог нельзя. Не потому, что расчёт неверен, —
# он верен, — а потому, что считать не на чем: одна находка сдвигает совет на
# сотни символов, и получается порог, подогнанный под случайную неделю.
#
# Пять дней из семи — из того же соображения, что и здоровье сбора в Фазе 5:
# неделя, в которую радар отработал два дня, ничего не измеряет. Десять
# размеченных находок — величина договорная и честно такая: меньше десятка
# случаев не делят пополам ничего, кроме самих себя.
ENOUGH_DAYS = 5
ENOUGH_MARKED = 10

# Хвост «[1929] 2026-08-19 07:45 · » впереди публикации из канала. Для разметки
# он полезен, для сравнения двух публикаций между собой — мешает: у одного и
# того же текста в Telegram и во ВКонтакте номера разные.
POST_HEAD = re.compile(r"^\s*\[\d+\]\s*")
MARKS = re.compile(r"<дата>|<время>|<когда>|<счётчик>|<идентификатор>")


# ─────────────────────────── что берём и за какой срок ────────────────────────

def period(days: int, last: str | None) -> list[str]:
    """Дни калибровки.

    В отличие от недельной сводки, здесь неделя заканчивается сегодняшним днём,
    а не вчерашним: сводку читает человек по понедельникам, а калибровку — тот,
    кто только что посмотрел на сегодняшний прогон и хочет знать, что с ним
    делать.
    """
    end = date.fromisoformat(last) if last else date.today()
    return [(end - timedelta(days=step)).isoformat()
            for step in range(days - 1, -1, -1)]


def display(item: dict) -> str:
    """Имя источника так, как его пишет детектор: по нему сходятся сводки дня."""
    return f"{item['конкурент']} · {item['страница']}"


def pretty(name: str) -> str:
    """То же имя, но для человека: «Roistat · Telegram @roistat_com».

    Разделять пришлось потому, что имя работает ключом. Ключ должен совпадать
    со сводкой дня буква в букву, а человеку в отчёте нужно «блог и новости»,
    а не «blog».
    """
    who, sep, page = name.rpartition(" · ")
    return f"{who}{sep}{notify.page_name(page)}" if sep else name


def gather(days: list[str], rules: dict) -> tuple[list[dict], dict]:
    """Все находки за период плюс то, что о периоде известно помимо находок."""
    findings: list[dict] = []
    known: set[str] = set()          # все источники, которых радар за период касался
    empty = 0                        # перестановки: не находки, но их полезно считать
    parsed_days: list[str] = []

    for day in days:
        summary = notify.day_summary(day)
        if summary:
            parsed_days.append(day)
            for key in ("только перестановка", "без изменений", "точка отсчёта"):
                known.update(summary.get(key, []))
            for key in ("в дайджест", "ниже порога"):
                for row in summary.get(key, []):
                    if isinstance(row, dict):
                        known.add(f"{row.get('конкурент')} · {row.get('страница')}")

        items = notify.day_deltas(day)
        critical, usual, minor, shuffled = notify.sort_out(items, rules)
        empty += len(shuffled)
        for item in shuffled:
            known.add(display(item))

        for klass, stack in (("критично", critical), ("в сводку", usual),
                             ("мелочь", minor)):
            for item in stack:
                verdict = item["приговор"]
                known.add(display(item))
                findings.append({
                    "день": day,
                    "ключ": f"{item['домен']}|{item['страница']}",
                    "имя": display(item),
                    "конкурент": item["конкурент"],
                    "домен": item["домен"],
                    "страница": item["страница"],
                    "адрес": item.get("адрес"),
                    "класс": klass,
                    "объём": int(item["разница"].get("затронуто символов", 0)),
                    "числа": notify.numbers_moved(item),
                    "канал": (classify.page_kind(item["страница"])
                              == classify.CHANNEL_PAGE),
                    "правила": list(verdict.rules),
                    "строки": list(item.get("кратко") or []),
                    "добавлено": list(item["разница"].get("добавлено", [])),
                    "удалено": list(item["разница"].get("удалено", [])),
                })

    return findings, {"дней с разбором": parsed_days, "перестановок": empty,
                      "источников известно": known}


def health(days: list[str]) -> dict:
    """Здоровье периода: в какие дни радар работал и сколько снял.

    Калибровать порог по неделе, в которой радар отработал два дня из семи,
    нельзя — и это надо видеть до таблиц, а не после них.
    """
    worked, collected, missing = 0, [], []
    for day in days:
        path = RUNS / f"{day}.json"
        if not path.exists():
            missing.append(day)
            continue
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            missing.append(day)
            continue
        worked += 1
        totals = run.get("totals") or {}
        ok = int(totals.get("изменилось", 0)) + int(totals.get("без изменений", 0))
        collected.append(ok)
    return {"дней": len(days), "отработал": worked, "нет прогона": missing,
            "снято в среднем": round(sum(collected) / len(collected)) if collected else 0}


# ─────────────────────────── лист разметки ────────────────────────────────────

HEADER = """# Лист разметки недели обкатки — Фаза 6.
#
# Здесь лежит всё, что радар нашёл за неделю. Против каждой находки надо
# поставить одно слово в поле «вердикт»:
#
#     сигнал — про это стоило сказать. Конкурент правда что-то сделал.
#     шум    — говорить не стоило. Само поменялось, или мелочь, или не про нас.
#     ?      — ещё не смотрел.
#
# Больше ничего делать не нужно: остальное посчитает python tools/calibrate.py.
# Поле «заметка» — свободное, для себя; программа его сохраняет, но не читает.
#
# Файл можно перезаписывать сколько угодно: python tools/calibrate.py --razmetka
# дописывает новые находки и не трогает уже проставленные вердикты.
#
# Почему разметка руками. Сигнал от шума отличает не объём изменения, а смысл:
# «цена выросла на 40%» — восемь символов и главный сигнал системы, а «на
# странице повернулась карусель отзывов» — четыреста символов и ничего. Никакая
# автоматика этого не решит, потому что решение зависит от того, зачем человеку
# радар, а не от того, что случилось на странице.
"""


def load_sheet() -> dict:
    """Прочитать уже проставленные вердикты: (день, ключ) → вердикт и заметка."""
    if not SHEET.exists():
        return {}
    try:
        raw = yaml.safe_load(SHEET.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        sys.exit(f"Лист разметки не читается: {error}\n"
                 f"Поправьте {SHEET.name} или удалите его — он соберётся заново.")
    out = {}
    for day, rows in raw.items():
        for row in rows or []:
            key = (str(day), str(row.get("ключ")))
            out[key] = {"вердикт": str(row.get("вердикт", UNMARKED)).strip().lower(),
                        "заметка": row.get("заметка") or ""}
    return out


def write_sheet(findings: list[dict], marks: dict, dry: bool) -> tuple[int, int]:
    """Собрать лист разметки заново, сохранив то, что человек уже отметил."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    added = 0
    for item in findings:
        key = (item["день"], item["ключ"])
        mark = marks.get(key)
        if mark is None:
            added += 1
        by_day[item["день"]].append({
            "находка": pretty(item["имя"]),
            "ключ": item["ключ"],
            "класс": item["класс"],
            "объём": item["объём"],
            "адрес": item["адрес"],
            "видно": [wording.shorten(line, 200) for line in item["строки"][:6]],
            "вердикт": (mark or {}).get("вердикт") or UNMARKED,
            "заметка": (mark or {}).get("заметка") or "",
        })

    body = {day: by_day[day] for day in sorted(by_day)}
    text = HEADER + "\n" + yaml.safe_dump(body, allow_unicode=True, sort_keys=False,
                                          default_flow_style=False, width=1000)
    if not dry:
        CALIB.mkdir(parents=True, exist_ok=True)
        SHEET.write_text(text, encoding="utf-8")
    return added, len(findings)


def verdict_of(item: dict, marks: dict) -> str:
    mark = marks.get((item["день"], item["ключ"]))
    value = (mark or {}).get("вердикт", UNMARKED)
    return value if value in VERDICTS else UNMARKED


# ─────────────────────────── что считаем ──────────────────────────────────────

def sweep(findings: list[dict], marks: dict, cfg: dict) -> list[dict]:
    """Что дошло бы до человека при каждом из порогов.

    Критичное в таблице участвует, но от порога не зависит: цена и слова из
    rules.yaml проходят мимо порога по решению Фазы 3, и калибровка этого не
    отменяет. Порог решает судьбу только обычных находок.
    """
    numbers_pass = bool(cfg.get("numbers_ignore_threshold", True))
    steps = sorted(set(LADDER) | {int(cfg.get("min_changed_chars", 120))})
    table = []
    for step in steps:
        row = {"порог": step, "дошло": 0, SIGNAL: 0, NOISE: 0, UNMARKED: 0,
               "потеряно сигналов": 0}
        for item in findings:
            passes = (item["класс"] == "критично" or item["объём"] >= step
                      or (item["числа"] and numbers_pass))
            mark = verdict_of(item, marks)
            if passes:
                row["дошло"] += 1
                row[mark] += 1
            elif mark == SIGNAL:
                row["потеряно сигналов"] += 1
        table.append(row)
    return table


def _body(line: str, domain: str, page: str) -> str:
    """Строка без того, что делает её уникальной: номера публикации, даты, часов."""
    masked = diffing.mask(POST_HEAD.sub("", line), domain, page)
    masked = MARKS.sub(" ", masked)
    return re.sub(r"\s+", " ", masked).strip(" ·•|,.:;–—-")


def candidates(findings: list[dict]) -> list[dict]:
    """Строки, которые шумят: приходят и уходят изо дня в день.

    Самая надёжная примета шума — не частота, а мигание: строка появилась в
    одном дне и исчезла в другом на том же источнике. Конкурент так не делает —
    так делает разметка страницы.
    """
    seen: dict[str, dict] = {}
    for item in findings:
        for side, lines in (("добавилась", item["добавлено"]),
                            ("исчезла", item["удалено"])):
            for line in lines:
                text = _body(line, item["домен"], item["страница"])
                if len(text) < 4 or diffing.is_noise(text):
                    continue
                slot = seen.setdefault(text, {"строка": line, "дни": set(),
                                              "источники": set(), "стороны": set(),
                                              "раз": 0})
                slot["раз"] += 1
                slot["дни"].add(item["день"])
                slot["источники"].add(item["имя"])
                slot["стороны"].add(side)

    out = []
    for text, slot in seen.items():
        flaps = len(slot["стороны"]) > 1
        if not flaps and len(slot["дни"]) < 2 and len(slot["источники"]) < 2:
            continue
        out.append({"строка": slot["строка"], "мигает": flaps,
                    "дней": len(slot["дни"]), "источников": len(slot["источники"]),
                    "раз": slot["раз"], "где": sorted(slot["источники"])[:3]})
    out.sort(key=lambda row: (row["мигает"], row["дней"], row["раз"]), reverse=True)
    return out[:CANDIDATES]


def twins(findings: list[dict]) -> list[dict]:
    """Одна и та же публикация, пришедшая двумя источниками одного конкурента.

    Roistat кладёт пост в Telegram и во ВКонтакте с разницей в две минуты; для
    радара это два изменения, для человека — одна новость, показанная дважды.
    """
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in findings:
        if item["канал"]:
            by_key[(item["день"], item["конкурент"])].append(item)

    out = []
    for (day, name), group in sorted(by_key.items()):
        if len(group) < 2:
            continue
        # Сравниваем начала строк, а не строки целиком: один и тот же пост в
        # Telegram и во ВКонтакте обрывается по-разному и заканчивается разными
        # ссылками. Шестидесяти знаков хватает, чтобы не спутать два разных
        # поста, и достаточно мало, чтобы поймать один и тот же.
        texts = [{_body(line, i["домен"], i["страница"])[:60] for line in i["добавлено"]}
                 for i in group]
        shared = set.intersection(*texts) if texts else set()
        shared = {line for line in shared if len(line) >= 40}
        if shared:
            out.append({"день": day, "конкурент": name,
                        "источники": [i["страница"] for i in group],
                        "совпало строк": len(shared),
                        "пример": wording.shorten(sorted(shared)[0], 90)})
    return out


def by_source(findings: list[dict], marks: dict, known: set[str]) -> list[dict]:
    """Таблица по источникам: кто сколько дал и чего именно."""
    rows: dict[str, dict] = {}
    for name in known:
        rows[name] = {"имя": name, "находок": 0, "дней": set(), "объёмы": [],
                      SIGNAL: 0, NOISE: 0, UNMARKED: 0, "критично": 0,
                      "последняя": ""}
    for item in findings:
        row = rows.setdefault(item["имя"], {"имя": item["имя"], "находок": 0,
                                            "дней": set(), "объёмы": [], SIGNAL: 0,
                                            NOISE: 0, UNMARKED: 0, "критично": 0,
                                            "последняя": ""})
        row["находок"] += 1
        row["дней"].add(item["день"])
        row["объёмы"].append(item["объём"])
        row[verdict_of(item, marks)] += 1
        row["критично"] += int(item["класс"] == "критично")
        row["последняя"] = max(row["последняя"], item["день"])
    out = list(rows.values())
    for row in out:
        row["дней"] = len(row["дней"])
        row["самое крупное"] = max(row["объёмы"]) if row["объёмы"] else 0
    out.sort(key=lambda row: (-row["находок"], row["имя"].lower()))
    return out


# ─────────────────────────── как это показать ─────────────────────────────────

def head(text: str) -> list[str]:
    return ["", f"── {text} " + "─" * max(0, 70 - len(text)), ""]


def report(days: list[str], findings: list[dict], extra: dict, marks: dict,
           cfg: dict, cal: dict, show_lines: bool) -> list[str]:
    out: list[str] = []
    ok = health(days)
    total = len(findings)
    counted = Counter(verdict_of(item, marks) for item in findings)
    classes = Counter(item["класс"] for item in findings)

    out += [f"Калибровка радара · {days[0]} — {days[-1]} ({len(days)} дней)"]
    if cal.get("mode"):
        until = cal.get("until")
        out += [f"Режим обкатки включён{f', до {until}' if until else ''}: "
                "сообщения помечены и не считаются боевой рассылкой."]
    else:
        out += ["Режим обкатки выключен: радар работает боевым. "
                "Калибровать можно и так, но сообщения уже уходят как настоящие."]

    out += head("Что было в эти дни")
    out += ["  Радар отработал: "
            + notify.plural(ok["отработал"], "день", "дня", "дней")
            + f" из {ok['дней']}, снимал в среднем "
            + notify.plural(ok["снято в среднем"], "источник", "источника",
                            "источников") + "."]
    if ok["нет прогона"]:
        out += [f"  Без прогона: {', '.join(ok['нет прогона'])} — "
                "за эти дни неизвестно ничего, и в расчёт они не идут."]
    out += [f"  Источников, которых радар касался: {len(extra['источников известно'])}.",
            f"  Находок: {total} — критичных {classes['критично']}, "
            f"в сводку {classes['в сводку']}, мелочи {classes['мелочь']}.",
            f"  Пустых сравнений (перестановка строк): {extra['перестановок']} — "
            "это не находки и в расчёт не идут."]

    if not total:
        out += ["", "  Находок нет вовсе. Это результат, а не пустая таблица:",
                "  либо у конкурентов правда тихо, либо радар не собирал. "
                "Строка выше отвечает, что из двух."]
        return out

    out += head("Разметка")
    if not SHEET.exists():
        out += ["  Листа разметки нет. Без него видно объём находок, но не их качество,",
                "  а калибруется именно качество. Завести: python tools/calibrate.py --razmetka"]
    else:
        out += [f"  Размечено: {counted[SIGNAL] + counted[NOISE]} из {total}. "
                f"Сигнал: {counted[SIGNAL]}, шум: {counted[NOISE]}, "
                f"не смотрели: {counted[UNMARKED]}."]
        if counted[SIGNAL] + counted[NOISE]:
            share = 100 * counted[SIGNAL] / (counted[SIGNAL] + counted[NOISE])
            out += [f"  Доля сигнала среди размеченного: {share:.0f}%."]
        if counted[UNMARKED]:
            out += [f"  Пока не размечено {counted[UNMARKED]} находок — "
                    f"числа ниже настолько же неполны. Файл: calibration/{SHEET.name}"]

    out += head("Порог: что дошло бы до человека")
    out += ["   порог   дошло   сигнал   шум   не смотрели   потеряно сигналов"]
    current = int(cfg.get("min_changed_chars", 120))
    for row in sweep(findings, marks, cfg):
        mark = " ← сейчас" if row["порог"] == current else ""
        out += [f"  {row['порог']:>6}  {row['дошло']:>6}  {row[SIGNAL]:>7}  "
                f"{row[NOISE]:>4}  {row[UNMARKED]:>12}  {row['потеряно сигналов']:>17}"
                + mark]
    out += ["",
            "  Критичное в таблице участвует, но от порога не зависит: цена и слова",
            "  из rules.yaml проходят мимо порога по решению Фазы 3."]

    out += head("Кандидаты в шумодав")
    found = candidates(findings)
    if not found:
        out += ["  Повторяющихся строк нет: за эти дни ни одна строка не приходила",
                "  дважды. Дописывать в noise.yaml нечего — и это тоже ответ."]
    for row in found:
        flag = "мигает" if row["мигает"] else "повторяется"
        out += [f"  {flag:<12} дней {row['дней']}, источников {row['источников']}, "
                f"раз {row['раз']}",
                f"               {wording.shorten(row['строка'], 100)}",
                f"               где: {', '.join(row['где'])}"]

    pairs = twins(findings)
    if pairs:
        out += head("Одна новость двумя источниками")
        for row in pairs:
            out += [f"  {row['день']}  {row['конкурент']}: "
                    f"{' и '.join(notify.page_name(p) for p in row['источники'])} — "
                    f"совпало строк: {row['совпало строк']}",
                    f"               {row['пример']}"]
        out += ["",
                "  В сводке это выглядит как два разных изменения у одного конкурента.",
                "  Решение фазы: либо убрать один из двух каналов из sources.yaml,",
                "  либо склеивать такие пары при сборке сводки."]

    out += head("Источники")
    rows = by_source(findings, marks, extra["источников известно"])
    loud = [row for row in rows if row["находок"]]
    quiet = [row for row in rows if not row["находок"]]
    out += ["   находок  дней  крупнейшее  сигнал/шум/?   источник"]
    for row in loud:
        out += [f"  {row['находок']:>8}  {row['дней']:>4}  {row['самое крупное']:>10}  "
                f"{row[SIGNAL]:>6}/{row[NOISE]}/{row[UNMARKED]:<6}  {pretty(row['имя'])}"]
    out += ["", f"  Молчали все дни: {len(quiet)} источников."]
    if quiet:
        out += ["  " + wording.shorten(", ".join(pretty(r["имя"]) for r in quiet), 300)]

    junk = [row for row in loud if row[NOISE] >= 3 and row[SIGNAL] == 0]
    if junk:
        out += ["", "  Только шум за весь период (кандидаты на выброс из sources.yaml):"]
        for row in junk:
            out += [f"    {pretty(row['имя'])}: {row[NOISE]} находок, все шум"]

    channels = [item for item in findings if item["канал"]]
    if channels:
        out += ["", f"  Из каналов соцсетей: {len(channels)} находок из {total}. "
                "Каждая новая публикация —",
                "  изменение по определению, и это отдельный разговор при выборе порога."]

    out += head("Что из этого следует")
    out += recommend(findings, marks, cfg, counted, ok["отработал"], len(days))
    if show_lines:
        out += head("Находки построчно")
        for item in sorted(findings, key=lambda i: (i["день"], i["имя"])):
            out += [f"  {item['день']}  {pretty(item['имя'])}  "
                    f"[{item['класс']}, {item['объём']} симв., "
                    f"{verdict_of(item, marks)}]"]
            for line in item["строки"][:6]:
                out += [f"        {wording.shorten(line, 110)}"]
    return out


def recommend(findings: list[dict], marks: dict, cfg: dict, counted: Counter,
              worked: int, days: int) -> list[str]:
    """Вывод по числам. Решение всё равно за человеком — он и несёт последствия."""
    current = int(cfg.get("min_changed_chars", 120))
    marked = counted[SIGNAL] + counted[NOISE]
    if marked == 0:
        return ["  Считать не по чему: не размечено ни одной находки.",
                "  Пять минут разметки дают неделю осмысленной работы радара —",
                "  без них порог останется догадкой из плана, какой и был."]

    table = sweep(findings, marks, cfg)
    clean = [row for row in table if row["потеряно сигналов"] == 0]
    # Из порогов, которые не теряют ни одного сигнала, берём самый низкий из тех,
    # что отсекают весь отсекаемый шум. Самый высокий отсёк бы ровно столько же,
    # но был бы подогнан под эту неделю: на следующей он молча съест сигнал,
    # которого в этой не было.
    quietest = min((row[NOISE] for row in clean), default=None)
    best = min((row for row in clean if row[NOISE] == quietest),
               key=lambda row: row["порог"], default=None)
    now = next(row for row in table if row["порог"] == current)

    out = []
    if best and best["порог"] > current and best[NOISE] < now[NOISE]:
        out += [f"  Порог можно поднять со {current} до {best['порог']} символов: "
                "это отсекает "
                + notify.plural(now[NOISE] - best[NOISE], "шумную находку",
                                "шумные находки", "шумных находок"),
                "  и не теряет ни одного размеченного сигнала."]
    elif now["потеряно сигналов"]:
        lower = [row for row in table if row["потеряно сигналов"] == 0]
        target = max(lower, key=lambda row: row["порог"])["порог"] if lower else 0
        out += [f"  Порог {current} теряет "
                + notify.plural(now["потеряно сигналов"], "размеченный сигнал",
                                "размеченных сигнала", "размеченных сигналов")
                + f". Опустить до {target}.",
                "  Потерянный сигнал дороже лишней строки в сводке: сводку человек",
                "  прочтёт и пропустит мимо, а пропущенное подорожание не вернуть."]
    else:
        out += [f"  Порог {current} на этих данных ведёт себя правильно: "
                "пропускает "
                + notify.plural(now[NOISE], "шумную находку", "шумные находки",
                                "шумных находок") + ", сигналов не теряет.",
                "  Оснований трогать его нет."]

    if counted[UNMARKED]:
        out += ["  Вывод предварительный: "
                + notify.plural(counted[UNMARKED], "находка", "находки", "находок")
                + " без оценки."]

    # Главная защита фазы — от неё самой. Расчёт выше верен на любых числах, и
    # именно поэтому на двух днях он с полной уверенностью посоветует порог,
    # подогнанный под эти два дня. Пропустить такой совет в решение — значит
    # заменить догадку из плана догадкой с таблицей, а это хуже: у таблицы вид
    # измерения.
    thin = []
    if worked < ENOUGH_DAYS:
        thin.append("радар отработал "
                    + notify.plural(worked, "день", "дня", "дней")
                    + f" из {days}")
    if marked < ENOUGH_MARKED:
        thin.append("размечено "
                    + notify.plural(marked, "находка", "находки", "находок")
                    + f", а нужно хотя бы {ENOUGH_MARKED}")
    if thin:
        out = ["  Порог менять рано: " + ", ".join(thin) + ".",
               "  Ниже — то, на что эти данные указывают. Это не вывод недели, а",
               "  предварительная картина: одна находка сдвигает её на сотни символов.",
               ""] + ["  " + line.strip() for line in out] + [
               "",
               "  Что делать: продлить обкатку — calibration.until в config.yaml —",
               "  и вернуться к таблице, когда данных станет больше."]

    out += ["", "  Решение принимает человек. Программа считает, а не постановляет:",
            "  цена ошибки в обе стороны разная, и знает её только владелец радара."]
    return out


# ─────────────────────────── запуск ───────────────────────────────────────────

def main() -> int:
    console.setup()
    ap = argparse.ArgumentParser(description="Калибровка радара на живых данных")
    ap.add_argument("--days", type=int, default=7, help="сколько дней брать")
    ap.add_argument("--to", help="последний день периода (по умолчанию сегодня)")
    ap.add_argument("--razmetka", action="store_true",
                    help="завести или дополнить лист разметки")
    ap.add_argument("--lines", action="store_true",
                    help="показать строки каждой находки")
    ap.add_argument("--dry-run", action="store_true",
                    help="ничего не записывать на диск")
    args = ap.parse_args()

    if args.days < 1:
        sys.exit("Дней в периоде должно быть хотя бы один.")

    cfg = {**{"min_changed_chars": 120, "numbers_ignore_threshold": True},
           **((yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
               or {}).get("detect") or {})}
    cal = notify.load_calibration()
    rules = classify.load_rules(ROOT / "rules.yaml")

    days = period(args.days, args.to)
    findings, extra = gather(days, rules)
    marks = load_sheet()

    if args.razmetka:
        added, total = write_sheet(findings, marks, args.dry_run)
        where = f"calibration/{SHEET.name}"
        if args.dry_run:
            print(f"Холостой запуск: лист разметки не записан. Было бы {total} "
                  f"находок, из них новых {added}.")
        else:
            print(f"Лист разметки: {where} — находок {total}, "
                  f"из них новых {added}.")
            print("Проставьте «сигнал» или «шум» в поле «вердикт» и запустите "
                  "python tools/calibrate.py")
        marks = load_sheet()

    lines = report(days, findings, extra, marks, cfg, cal, args.lines)
    for line in lines:
        print(line)

    if not args.dry_run:
        CALIB.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = CALIB / f"otchet-{stamp}.md"
        path.write_text(
            f"# Калибровка радара · {days[0]} — {days[-1]}\n\n"
            f"Собрано {datetime.now(timezone.utc).isoformat(timespec='minutes')} UTC "
            "программой tools/calibrate.py.\n\n```\n"
            + "\n".join(lines) + "\n```\n", encoding="utf-8")
        print(f"\nОтчёт: calibration/{path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
