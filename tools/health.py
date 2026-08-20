#!/usr/bin/env python3
"""Здоровье сбора — Фаза 5. Отвечает на один вопрос: радар ещё смотрит?

Система наблюдения умирает тихо. Сборщик перестаёт заходить на сайты — токен
протух, сеть сменилась, планировщик отключили обновлением, — а человек видит
ровно то же, что и в спокойную неделю: сводку без изменений. «У конкурентов
ничего не происходит» и «никто туда не смотрел» выглядят одинаково, и разница
выясняется через месяц, когда конкурент уже поменял цены.

Поэтому в плане записано отдельной строкой: **сбор не прошёл два дня подряд —
алерт владельцу.** Это и делается здесь.

Что считается «сбор не прошёл». Не то же самое, что «код возврата сборщика не
ноль»: сборщик отдаёт единицу, если хоть один источник из шестидесяти двух
ответил ошибкой, а один упавший сайт конкурента — это будни, а не поломка
радара. День провален, если:

* отчёта прогона за этот день нет вовсе — расписание не сработало, машина была
  выключена, программа упала до записи отчёта;
* или отчёт есть, но снята меньше чем половина источников — так выглядит
  лежащая сеть, слетевший DNS, кончившийся диск. Отказ одного-двух сайтов сюда
  не попадает: про них рассказывает недельная сводка отдельным списком
  «не собирается с такого-то числа», и это работа Фазы 4, а не здешняя.

Разделение простое: **здесь — здоровье радара, там — здоровье источника.**

Ещё одно правило, ради которого файл получился длиннее, чем «посчитать дни».
Провал, замеченный задним числом, тоже надо показать. Ноутбук был выключен
среду и четверг, в пятницу включился и всё собралось — тревоги нет, а два дня
наблюдения потеряны, и в сводке за неделю на их месте будет тишина, похожая на
спокойствие. Поэтому радар говорит и «сбор снова идёт, молчал два дня».

Зависимостей у этого файла нет намеренно. Он должен работать в тот день, когда
сломалось всё остальное, поэтому здесь только стандартная библиотека — даже
русские даты и склонения написаны заново, а не взяты из notify.py.
"""

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

# Статусы сборщика (collect.py), при которых снимок получен. Всё остальное —
# ошибка, бот-защита или подозрительно короткая страница; «пропущено» стоит
# особняком: это источники, которые радар и не собирался снимать.
GOT = {"точка отсчёта", "без изменений", "изменилось"}
SKIPPED = "пропущено"

JOURNAL = "health.json"

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


def ru_date(day) -> str:
    parsed = date.fromisoformat(day) if isinstance(day, str) else day
    return f"{parsed.day} {MONTHS[parsed.month - 1]}"


def plural(number: int, one: str, few: str, many: str) -> str:
    tail, hundred = number % 10, number % 100
    if tail == 1 and hundred != 11:
        return f"{number} {one}"
    if 2 <= tail <= 4 and not 12 <= hundred <= 14:
        return f"{number} {few}"
    return f"{number} {many}"


@dataclass
class Day:
    """Итог одного дня глазами health-check: собрали или нет и почему."""
    day: date
    passed: bool
    why: str
    known: bool = False      # отчёт прогона за этот день существует
    got: int = 0             # источников снято
    total: int = 0           # источников проверялось

    @property
    def stamp(self) -> str:
        return self.day.isoformat()


@dataclass
class Verdict:
    """Что радар скажет человеку про своё здоровье сегодня."""
    kind: str                       # норма | тревога | повтор | починилось | точка отсчёта
    text: str = ""                  # сообщение владельцу; пусто — молчим
    fails: list = field(default_factory=list)
    last_ok: date | None = None
    silent_days: int = 0

    @property
    def speak(self) -> bool:
        return bool(self.text)


def read_day(runs: Path, day: date, min_ok_share: float) -> Day:
    """Прошёл ли сбор за этот день. Судим по отчёту прогона, а не по коду возврата."""
    path = runs / f"{day.isoformat()}.json"
    if not path.exists():
        return Day(day, False, "прогона не было")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        return Day(day, False, f"отчёт прогона не читается ({error})", known=True)

    items = [i for i in report.get("items", []) if i.get("status") != SKIPPED]
    if not items:
        return Day(day, False, "в отчёте прогона нет ни одного источника", known=True)

    got = sum(1 for i in items if i.get("status") in GOT)
    total = len(items)
    if got / total >= min_ok_share:
        return Day(day, True, f"снято {got} из {total}", True, got, total)
    return Day(day, False, f"снято только {got} из {total}", True, got, total)


def history(runs: Path, today: date, depth: int, min_ok_share: float) -> list:
    """Последние `depth` дней по порядку, от старого к сегодняшнему."""
    return [read_day(runs, today - timedelta(days=back), min_ok_share)
            for back in range(depth - 1, -1, -1)]


def tail_fails(days: list) -> list:
    """Хвост подряд идущих провалов в конце списка."""
    fails = []
    for day in reversed(days):
        if day.passed:
            break
        fails.append(day)
    return list(reversed(fails))


def read_journal(runs: Path) -> dict:
    path = runs / JOURNAL
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Побитый журнал — не повод остановить прогон. Заведём заново; худшее,
        # что случится, — одно лишнее сообщение о тревоге.
        return {}


def write_journal(runs: Path, journal: dict) -> None:
    runs.mkdir(parents=True, exist_ok=True)
    (runs / JOURNAL).write_text(
        json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")


def check(days: list, journal: dict, cfg: dict, today: date) -> Verdict:
    """Решить, что сказать владельцу про здоровье сбора. Ничего не отправляет.

    Разделение сознательное: решение проверяется на выдуманных днях в
    tools/schedule_check.py, а отправка — живая и проверяется один раз руками.
    """
    fail_days = int(cfg["fail_days"])
    repeat = int(cfg["repeat_alert_days"])
    fails = tail_fails(days)
    last_ok = max((d.day for d in days if d.passed), default=None)
    alarm = journal.get("тревога")

    # Первый прогон с журналом — точка отсчёта, как первый снимок страницы.
    # Иначе расписание, поставленное через месяц после последнего ручного
    # прогона, первым же делом сообщит о месяце молчания, которого не было.
    if not journal.get("заведён"):
        return Verdict("точка отсчёта", "", fails, last_ok, len(fails))

    today_failed = bool(fails) and fails[-1].day == today

    if today_failed and len(fails) >= fail_days:
        if not alarm:
            return Verdict("тревога", alarm_text(fails, last_ok),
                           fails, last_ok, len(fails))
        told = date.fromisoformat(str(alarm.get("сказали", ""))[:10])
        if (today - told).days >= repeat:
            return Verdict("повтор", alarm_text(fails, last_ok, again=True),
                           fails, last_ok, len(fails))
        return Verdict("норма", "", fails, last_ok, len(fails))

    if not today_failed:
        gap = tail_fails(days[:-1]) if len(days) > 1 else []
        already = journal.get("последняя починка") == today.isoformat()
        if (alarm or len(gap) >= fail_days) and not already:
            return Verdict("починилось", recovery_text(days[-1], gap), gap,
                           last_ok, len(gap))

    return Verdict("норма", "", fails, last_ok, len(fails))


def alarm_text(fails: list, last_ok, again: bool = False) -> str:
    """Сообщение «радар ослеп». Коротко, по делу и без предложений что-то нажать."""
    if again:
        head = ("\U0001F534 <b>Радар всё ещё не собирает: "
                + plural(len(fails), "день", "дня", "дней") + " подряд</b>")
    else:
        head = ("\U0001F534 <b>Радар: сбор не проходит "
                + plural(len(fails), "день", "дня", "дней") + " подряд</b>")

    lines = [head, ""]
    for day in fails:
        lines.append(f"{ru_date(day.day)} — {day.why}")
    lines.append("")
    if last_ok:
        lines.append(f"Последний удачный сбор: {ru_date(last_ok)}.")
    else:
        lines.append("Удачных сборов за последние две недели нет вовсе.")
    lines.append("")
    lines.append("Пока сбор не идёт, пустая сводка не значит «у конкурентов "
                 "тихо» — она значит «никто не смотрел».")
    lines.append("")
    lines.append("Что смотреть: журнал последнего прогона в папке work/logs/, "
                 "запуск руками — <code>python daily.py</code>.")
    return "\n".join(lines)


def recovery_text(today: Day, gap: list) -> str:
    """Сообщение «снова вижу». Нужно не меньше тревожного: молчание кончилось."""
    lines = ["\U0001F7E2 <b>Радар: сбор снова идёт</b>", ""]
    lines.append(f"{ru_date(today.day)} — {today.why}.")
    if gap:
        days = ", ".join(ru_date(d.day) for d in gap)
        lines.append("")
        lines.append("Радар молчал " + plural(len(gap), "день", "дня", "дней")
                     + f": {days}.")
        lines.append("Изменения за эти дни не потеряны: детектор сравнивает "
                     "свежий снимок с последним, какой есть, и пишет, какой "
                     "между ними разрыв. Потеряно только то, что конкурент "
                     "успел выложить и убрать внутри этих дней.")
    return "\n".join(lines)


def remember(journal: dict, verdict: Verdict, today: date, when: str,
             message_ids) -> dict:
    """Записать в журнал то, о чём человека уже побеспокоили."""
    journal.setdefault("заведён", today.isoformat())
    if verdict.last_ok:
        journal["последний удачный сбор"] = verdict.last_ok.isoformat()

    if verdict.kind in ("тревога", "повтор"):
        journal["тревога"] = {
            "с какого дня": verdict.fails[0].stamp if verdict.fails else today.isoformat(),
            "провалов подряд": len(verdict.fails),
            "сказали": when,
            "сообщения": message_ids or [],
        }
    elif verdict.kind == "починилось":
        journal["тревога"] = None
        journal["последняя починка"] = today.isoformat()
    return journal
