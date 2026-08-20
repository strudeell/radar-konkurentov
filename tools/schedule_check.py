#!/usr/bin/env python3
"""Проверка расписания: что радар скажет, когда перестанет собирать.

Зачем это отдельной программой. Health-check нельзя проверить, дождавшись
поломки: он для того и написан, чтобы про поломку узнали в первый же день, а
узнаем мы, работает ли он, ровно тогда, когда сообщение не придёт. Ждать двух
суток без сбора, чтобы посмотреть на алерт, — плохой план и по времени, и по
смыслу: пропущенные двое суток наблюдения назад не вернуть.

Поэтому дни здесь выдуманные, а правила настоящие. В пустой папке создаются
отчёты прогонов такого же вида, какой пишет collect.py, и через тот же
tools/health.py прогоняются одиннадцать положений дел: от «сегодня всё
собралось» до «ноутбук был выключен трое суток». Каждый случай объявляет, чего от него
ждут; не совпало — ненулевой код возврата. Это регрессионная проверка, а не
демонстрация.

Отдельно проверяется выбор дня недельной сводки — включая досылку за
пропущенный понедельник, потому что промахнуться там легко, а заметить трудно:
сводка просто не приходит, и это выглядит как «за неделю ничего не было».

Запуск:

    python tools/schedule_check.py             прогнать все случаи
    python tools/schedule_check.py --verbose   показать сообщения целиком
"""

import argparse
import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import console  # noqa: E402
import health  # noqa: E402

import daily  # noqa: E402

TODAY = date(2026, 8, 19)          # среда
CFG = {"fail_days": 2, "min_ok_share": 0.5, "repeat_alert_days": 3,
       "look_back_days": 14, "digest_weekday": 1, "digest_catchup": True,
       "digest_days": 7}

# Журнал, в котором уже что-то происходило: без ключа «заведён» health-check
# считает прогон первым и молчит, и это его правильное поведение.
LIVED = {"заведён": "2026-08-01"}


def write_run(runs: Path, day: date, got: int, bad: int = 0) -> None:
    """Отчёт прогона такого же вида, какой пишет collect.py."""
    items = ([{"competitor": f"К{n}", "page": "home", "status": "без изменений"}
              for n in range(got)]
             + [{"competitor": f"П{n}", "page": "home", "status": "ошибка"}
                for n in range(bad)])
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{day.isoformat()}.json").write_text(
        json.dumps({"date": day.isoformat(), "items": items}, ensure_ascii=False),
        encoding="utf-8")


def alarm_days_ago(days: int, since: date) -> dict:
    """Журнал, в котором тревога уже объявлена столько-то дней назад."""
    told = TODAY - timedelta(days=days)
    return {**LIVED, "тревога": {"с какого дня": since.isoformat(),
                                 "провалов подряд": 2,
                                 "сказали": f"{told.isoformat()}T06:40:00+00:00",
                                 "сообщения": [1]}}


# Каждый случай: как выглядели последние дни и чего ждём от health-check.
# «дни» — сколько источников снялось в этот день, считая от сегодняшнего назад.
# None означает «прогона в этот день не было вовсе».
CASES = [
    {
        "name": "обычный день: всё собралось",
        "days": [60, 60, 60], "journal": LIVED,
        "expect": "норма",
    },
    {
        "name": "первый прогон по расписанию — точка отсчёта",
        "days": [60, None, None, None], "journal": {},
        "expect": "точка отсчёта",
        "why": "журнал заводится сегодня; месяц до установки радар и не должен был смотреть",
    },
    {
        "name": "один день без сбора — ещё не тревога",
        "days": [None, 60, 60], "journal": LIVED,
        "expect": "норма",
        "why": "порог из плана — два дня подряд, и он тут не взят",
    },
    {
        "name": "два дня подряд без сбора",
        "days": [None, None, 60], "journal": LIVED,
        "expect": "тревога",
        "must": ["18 августа", "19 августа", "17 августа"],
    },
    {
        "name": "тревога объявлена вчера, сбора по-прежнему нет",
        "days": [None, None, None, 60],
        "journal": alarm_days_ago(1, date(2026, 8, 17)),
        "expect": "норма",
        "why": "о поломке уже сказали; каждый день напоминать — приучить не читать",
    },
    {
        "name": "тревога объявлена три дня назад, сбора всё нет",
        "days": [None] * 5 + [60],
        "journal": alarm_days_ago(3, date(2026, 8, 14)),
        "expect": "повтор",
        "must": ["всё ещё не собирает"],
    },
    {
        "name": "после тревоги сбор пошёл",
        "days": [60, None, None, 60],
        "journal": alarm_days_ago(1, date(2026, 8, 17)),
        "expect": "починилось",
        "must": ["снова идёт", "17 августа", "18 августа"],
    },
    {
        "name": "ноутбук был выключен двое суток, тревоги никто не видел",
        "days": [60, None, None, 60], "journal": LIVED,
        "expect": "починилось",
        "why": "тревоги не было — сказать было некому; но два дня наблюдения потеряны",
    },
    {
        "name": "о починке уже сказали сегодня, прогон запустили второй раз",
        "days": [60, None, None, 60],
        "journal": {**LIVED, "последняя починка": TODAY.isoformat()},
        "expect": "норма",
        "why": "второй запуск в тот же день не должен слать второе сообщение",
    },
    {
        "name": "сеть легла: снялось 10 источников из 60",
        "days": [(10, 50), None, 60], "journal": LIVED,
        "expect": "тревога",
        "must": ["снято только 10 из 60"],
    },
    {
        "name": "один сайт конкурента лежит, остальные сняты",
        "days": [(59, 1), (59, 1), 60], "journal": LIVED,
        "expect": "норма",
        "why": "здоровье источника — забота недельной сводки, а не health-check",
    },
]

# Выбор дня недельной сводки. «уже слали» — какие периоды лежат в журнале Фазы 4
# (ключ там — последний день недели, то есть воскресенье). «прогоны» — дни,
# за которые у радара есть отчёт сборщика; по умолчанию неделя собрана.
WEEK = [date(2026, 8, 10) + timedelta(days=n) for n in range(7)]   # 10–16 августа
DIGESTS = [
    {"name": "понедельник — день сводки", "day": date(2026, 8, 17),
     "sent": [], "expect": date(2026, 8, 17)},
    {"name": "вторник, сводку за понедельник не отправляли", "day": date(2026, 8, 18),
     "sent": [], "expect": date(2026, 8, 17),
     "why": "машина была выключена в понедельник; сводка досылается"},
    {"name": "вторник, сводка за понедельник уже ушла", "day": date(2026, 8, 18),
     "sent": ["2026-08-16"], "expect": None},
    {"name": "воскресенье, сводка за понедельник ушла", "day": date(2026, 8, 23),
     "sent": ["2026-08-16"], "expect": None},
    {"name": "воскресенье, понедельник пропущен целиком", "day": date(2026, 8, 23),
     "sent": [], "expect": date(2026, 8, 17),
     "why": "шесть дней без сводки — это неделя, о которой никто не рассказал"},
    {"name": "первый понедельник после установки: неделя без единого прогона",
     "day": date(2026, 8, 17), "sent": [], "runs": [], "expect": None,
     "why": "пустая сводка за неделю, которой у радара не было, читается как «у конкурентов тихо»"},
    {"name": "неделя, в которую радар собрал один день из семи",
     "day": date(2026, 8, 17), "sent": [], "runs": [date(2026, 8, 14)],
     "expect": date(2026, 8, 17),
     "why": "один собранный день — уже повод рассказать, а не молчать неделю"},
]


def run_case(case: dict, number: int, workdir: Path) -> dict:
    # Своя папка на каждый случай: дни предыдущего не должны в него протекать.
    runs = workdir / f"case{number}" / "runs"
    for back, got in enumerate(case["days"]):
        if got is None:
            continue
        ok, bad = got if isinstance(got, tuple) else (got, 0)
        write_run(runs, TODAY - timedelta(days=back), ok, bad)

    days = health.history(runs, TODAY, CFG["look_back_days"], CFG["min_ok_share"])
    verdict = health.check(days, dict(case["journal"]), CFG, TODAY)
    missing = [must for must in case.get("must", []) if must not in verdict.text]
    return {"case": case, "verdict": verdict, "missing": missing,
            "ok": verdict.kind == case["expect"] and not missing}


def run_digest(case: dict, number: int, workdir: Path) -> dict:
    folder = workdir / f"digest{number}"
    (folder / "notify").mkdir(parents=True, exist_ok=True)
    (folder / "notify" / "journal.json").write_text(
        json.dumps({"алерты": {}, "сводки": {end: {} for end in case["sent"]}},
                   ensure_ascii=False), encoding="utf-8")
    runs = folder / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    for day in case.get("runs", WEEK):
        write_run(runs, day, 60)

    daily.NOTIFY, daily.RUNS = folder / "notify", runs
    day, why = daily.digest_plan(case["day"], CFG)
    return {"case": case, "day": day, "why": why, "ok": day == case["expect"]}


def main() -> int:
    console.setup()
    ap = argparse.ArgumentParser(description="Проверка расписания и health-check")
    ap.add_argument("--verbose", action="store_true",
                    help="показать сообщения владельцу целиком")
    args = ap.parse_args()

    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        print(f"Здоровье сбора. Сегодня {TODAY.isoformat()}, "
              f"порог {CFG['fail_days']} дня подряд.\n")
        for number, case in enumerate(CASES, 1):
            result = run_case(case, number, workdir)
            verdict = result["verdict"]
            mark = "как ожидали" if result["ok"] else "НЕ СОВПАЛО"
            if not result["ok"]:
                failed += 1
            print(f"  {mark:<11} {case['name']}")
            print(f"               ждали «{case['expect']}», вышло «{verdict.kind}»"
                  + (f", провалов подряд {verdict.silent_days}"
                     if verdict.silent_days else ""))
            if case.get("why"):
                print(f"               • {case['why']}")
            for must in result["missing"]:
                print(f"               НЕТ В СООБЩЕНИИ: «{must}»")
            if verdict.speak:
                lines = verdict.text.splitlines()
                shown = lines if args.verbose else lines[:3]
                for line in shown:
                    if line:
                        print(f"                 {line}")
                if len(shown) < len(lines):
                    print(f"                 … и ещё {len(lines) - len(shown)} строк")
            print()

        print("Выбор дня недельной сводки.\n")
        for number, case in enumerate(DIGESTS, 1):
            result = run_digest(case, number, workdir)
            mark = "как ожидали" if result["ok"] else "НЕ СОВПАЛО"
            if not result["ok"]:
                failed += 1
            print(f"  {mark:<11} {case['name']}")
            print(f"               ждали {case['expect']}, вышло {result['day']}"
                  f" — {result['why']}")
            if case.get("why"):
                print(f"               • {case['why']}")
            print()

    total = len(CASES) + len(DIGESTS)
    print(f"Случаев: {total}, разошлось с ожиданием: {failed}.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
