#!/usr/bin/env python3
"""Ежедневный прогон радара — Фаза 5. То, что запускает расписание.

Расписанию нельзя давать три команды подряд: планировщик Windows умеет запустить
одну программу и не умеет ни «если первая упала — вторую не запускать», ни
«по понедельникам ещё и четвёртую». Всё это живёт здесь, а планировщику
достаётся одна строка: раз в сутки запусти daily.py.

Что делает прогон, по порядку:

1. **Сбор** (collect.py) — обход всех источников, снимки на диск.
2. **Разбор** (detect.py) — сравнение со вчерашним, дельты и сводка дня.
3. **Уведомления** (notify.py) — срочное уходит сразу, остальное копится.
4. **Недельная сводка** (notify.py --digest) — по понедельникам. Если понедельник
   пропущен (машина была выключена), сводка досылается в первый же день, когда
   радар очнулся: неделя без сводки хуже, чем сводка, пришедшая во вторник.
5. **Здоровье сбора** — сбор не прошёл два дня подряд, владельцу уходит алерт.
   Правила — в tools/health.py, там же объяснено, что считается «не прошёл».

Три вещи, которые здесь важнее самой последовательности.

**Шаг, упавший с ошибкой, не отменяет следующий.** Единственное исключение —
сбор: если снимков нет, разбирать нечего, и цепочка останавливается. Всё
остальное идёт до конца. Уведомления, не отправленные из-за того, что споткнулся
разбор одного дня, — худший исход из возможных.

**Весь вывод пишется в work/logs/<дата>.log.** Планировщик показывает только код
возврата: «последний запуск: 0x1». По одному этому числу нельзя понять, сеть
легла или у конкурента поменялся сайт. Журнал прогона — единственное место, где
это видно, поэтому он пишется всегда, даже когда прогон запущен руками.

**Два прогона одновременно не пускаются.** Не ради данных — сборщик пережил бы, —
а ради чужих сайтов: два параллельных обхода превращают вежливую паузу между
запросами в её половину, и вежливость перестаёт быть вежливостью.

Запуск:

    python daily.py                  боевой прогон: то же, что делает расписание
    python daily.py --dry-run        прогнать цепочку вхолостую, ничего не слать
    python daily.py --no-digest      без недельной сводки
    python daily.py --health         только показать здоровье сбора и выйти
    python daily.py --plan           настройки расписания в JSON (для установщика)

Поставить прогон на расписание — tools/schedule.ps1, порядок в docs/RASPISANIE.md.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
from datetime import date, datetime, time as clock, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools"))

import yaml  # noqa: E402

import console  # noqa: E402
import health  # noqa: E402
import telegram  # noqa: E402

# Настройки человека — в config/, всё, что радар пишет сам, — в work/.
CONFIG = ROOT / "config"
WORK = ROOT / "work"

RUNS = WORK / "runs"
LOGS = WORK / "logs"
NOTIFY = WORK / "notify"
LOCK = RUNS / "daily.lock"

# Значения по умолчанию. Всё это можно менять в config/config.yaml, раздел schedule.
DEFAULTS = {
    # Время ежедневного сбора по UTC. Не на ровном часе и не в половину —
    # объяснение в config/config.yaml и в docs/RASPISANIE.md.
    "time_utc": "06:37",
    # Сколько ждать один шаг, прежде чем считать его зависшим. Полный обход
    # шестидесяти источников занимает три минуты; сорок — это запас на случай,
    # когда чужой сайт отвечает по таймауту раз за разом.
    "step_timeout_min": 40,
    # День недели для сводки: 1 — понедельник, по ISO.
    "digest_weekday": 1,
    "digest_catchup": True,
    # Здоровье сбора: сколько дней подряд без сбора — уже тревога.
    "fail_days": 2,
    "min_ok_share": 0.5,
    "repeat_alert_days": 3,
    "look_back_days": 14,
    # Журналы прогонов чистятся по тому же правилу, что и снимки.
    "keep_log_days": 90,
    "task_name": "RadarKonkurentov",
}


def load_config() -> dict:
    path = CONFIG / "config.yaml"
    if not path.exists():
        sys.exit("Не найден config/config.yaml рядом с daily.py.")
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = {**DEFAULTS, **(config.get("schedule") or {})}
    # Длина недели у сводки — настройка Фазы 4. Здесь она нужна ровно для
    # одного: понять, собирал ли радар хоть что-нибудь за ту неделю, о которой
    # его собираются заставить рассказать.
    cfg["digest_days"] = int((config.get("notify") or {}).get("digest_days", 7))
    return cfg


# ─────────────────────────── журнал прогона ───────────────────────────────────

class Log:
    """Вывод сразу в двух местах: на экран и в work/logs/<дата>.log.

    Под расписанием экрана нет вовсе — pythonw.exe запускается без окна, и
    sys.stdout там равен None. Печать в никуда роняет программу на первом же
    print, поэтому экран здесь необязателен, а файл обязателен.
    """

    def __init__(self, day: str, quiet: bool = False):
        LOGS.mkdir(parents=True, exist_ok=True)
        self.path = LOGS / f"{day}.log"
        self.file = self.path.open("a", encoding="utf-8")
        self.screen = None if quiet else sys.stdout

    def __call__(self, line: str = "") -> None:
        self.file.write(line + "\n")
        self.file.flush()
        if self.screen is not None:
            try:
                print(line)
            except (OSError, ValueError):
                self.screen = None      # окно закрыли посреди прогона

    def close(self) -> None:
        self.file.close()


def prune_logs(today: date, keep_days: int) -> int:
    """Журналы старше keep_days дней. Правило то же, что у снимков."""
    if not LOGS.exists():
        return 0
    edge = today - timedelta(days=int(keep_days))
    gone = 0
    for path in LOGS.glob("*.log"):
        try:
            when = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if when < edge:
            path.unlink()
            gone += 1
    return gone


# ─────────────────────────── замок ────────────────────────────────────────────

def take_lock(stale_sec: float) -> bool:
    """Занять замок. Чужой замок старше времени шага считается брошенным."""
    RUNS.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            held = json.loads(LOCK.read_text(encoding="utf-8"))
            started = datetime.fromisoformat(held["начат"])
            age = (datetime.now(timezone.utc) - started).total_seconds()
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            age = stale_sec + 1     # замок нечитаем — значит, брошен
        if age <= stale_sec:
            return False
    LOCK.write_text(json.dumps({
        "pid": os.getpid(),
        "начат": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, ensure_ascii=False), encoding="utf-8")
    return True


def drop_lock() -> None:
    try:
        LOCK.unlink(missing_ok=True)
    except OSError:
        pass


# ─────────────────────────── шаги цепочки ─────────────────────────────────────

def run_step(name: str, script: str, extra: list, log: Log,
             timeout_sec: float) -> dict:
    """Запустить одну программу радара, показать её вывод и запомнить итог.

    Почему отдельными процессами, а не импортом. Три причины, и все проверены
    на этом же проекте: упавший шаг не уносит с собой остальные; у каждого шага
    свой код возврата, а он здесь и есть главный сигнал; и запуск руками
    (`python detect.py`) остаётся ровно тем же самым, что делает расписание.
    """
    cmd = [sys.executable, str(ROOT / script)] + extra
    # PYTHONIOENCODING — не перестраховка, а лечение проверенной поломки:
    # вывод, перенаправленный в файл, Python кодирует в cp1251 и падает на
    # знаке «−» из отчёта детектора. Подробности — в tools/console.py.
    #
    # PYTHONUNBUFFERED — про другое, но тоже проверено на этом же прогоне:
    # программа, чей вывод уходит в канал, а не в окно, копит его блоками по
    # несколько килобайт. Трёхминутный обход шестидесяти сайтов при этом не
    # пишет в журнал ни строки до самого конца, а шаг, убитый по таймауту,
    # не пишет вообще ничего — то есть журнал молчит ровно тогда, когда он
    # нужнее всего. С этой переменной строки ложатся в журнал сразу.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}

    log("")
    log(f"── {name}: {script} {' '.join(extra)}".rstrip())
    started = datetime.now(timezone.utc)

    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1)
    except OSError as error:
        log(f"   не запустилось: {error}")
        return {"шаг": name, "команда": script, "код": 127,
                "секунд": 0.0, "итог": f"не запустилось: {error}"}

    # Сторож на случай зависшего шага: чужой сайт может держать соединение
    # открытым и не отдавать ни байта, и тогда чтение вывода не кончится само.
    killer = threading.Timer(timeout_sec, proc.kill)
    killer.start()
    try:
        for line in proc.stdout:
            log("   " + line.rstrip())
        proc.wait()
    finally:
        killer.cancel()

    spent = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
    timed_out = spent >= timeout_sec
    outcome = "остановлен по таймауту" if timed_out else f"код возврата {proc.returncode}"
    log(f"   {name}: {outcome}, {spent} с")
    return {"шаг": name, "команда": script, "код": proc.returncode,
            "секунд": spent, "итог": outcome, "таймаут": timed_out}


def sent_digests() -> dict:
    """Какие недельные сводки уже уходили. Память живёт в журнале Фазы 4."""
    path = NOTIFY / "journal.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("сводки", {}) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def digest_plan(day: date, cfg: dict) -> tuple:
    """Нужна ли сегодня сводка и за какой понедельник. Возвращает (дата, почему).

    Сводка привязана к понедельнику, а не к «каждым седьмым суткам»: человек
    читает её в начале недели, и неделя в ней — прошлая, целиком. Досылка нужна
    ровно потому же: пропущенный понедельник означает не «недели не было», а
    «неделя есть, а рассказать о ней некому».
    """
    weekday = int(cfg["digest_weekday"])
    if day.isoweekday() == weekday:
        when, why = day, "сегодня день недельной сводки"
    elif not cfg["digest_catchup"]:
        return None, "не день сводки"
    else:
        when = day - timedelta(days=(day.isoweekday() - weekday) % 7)
        gone = sent_digests()
        if (when - timedelta(days=1)).isoformat() in gone:
            return None, ("сводка за неделю по "
                          f"{health.ru_date(when - timedelta(days=1))} уже уходила")
        why = (f"сводку за {health.ru_date(when)} не отправляли — "
               "досылаем сегодня")

    # Неделя, в которой радар не собрал ни одного дня, сводки не заслуживает:
    # рассказывать в ней не о чем, а пустая сводка читается как «у конкурентов
    # тихо». Так выглядит первая неделя после установки — та, которой у радара
    # ещё не было, — и неделя, всю которую он пролежал. Про вторую человеку
    # скажет не сводка, а красная тревога health-check.
    end = when - timedelta(days=1)
    if not week_collected(end, int(cfg["digest_days"])):
        return None, (f"за неделю по {health.ru_date(end)} радар не собрал ни "
                      "одного дня — рассказывать не о чем")
    return when, why


def week_collected(end: date, days: int) -> bool:
    """Был ли за эту неделю хоть один прогон сборщика."""
    return any((RUNS / f"{(end - timedelta(days=back)).isoformat()}.json").exists()
               for back in range(days))


# ─────────────────────────── здоровье ─────────────────────────────────────────

def health_pass(cfg: dict, today: date, log: Log, dry: bool) -> dict:
    """Проверить, идёт ли сбор вообще, и сказать владельцу, если перестал."""
    days = health.history(RUNS, today, int(cfg["look_back_days"]),
                          float(cfg["min_ok_share"]))
    journal = health.read_journal(RUNS)
    verdict = health.check(days, journal, cfg, today)

    log("")
    log("── здоровье сбора")
    for day in days[-int(cfg["fail_days"]) - 3:]:
        mark = "собран" if day.passed else "ПРОВАЛ"
        log(f"   {day.stamp}  {mark:<7} {day.why}")
    if verdict.last_ok:
        log(f"   последний удачный сбор: {verdict.last_ok.isoformat()}")
    log(f"   вывод: {verdict.kind}")

    if not verdict.speak:
        if not dry:
            health.write_journal(
                RUNS, health.remember(journal, verdict, today, "", []))
        return {"вывод": verdict.kind, "провалов подряд": len(verdict.fails),
                "сказано": False}

    log("")
    for line in verdict.text.splitlines():
        log("   " + line)

    if dry:
        log("   (холостой прогон: сообщение не отправлено, журнал не тронут)")
        return {"вывод": verdict.kind, "провалов подряд": len(verdict.fails),
                "сказано": False}

    ids, told, why = [], False, ""
    try:
        bot = telegram.load(ROOT)
    except telegram.TelegramError as error:
        bot, why = None, f"доступ к боту настроен не до конца: {error}"
    if bot is None:
        why = why or "бот не настроен — сказать некому"
        log(f"   {why}")
    else:
        try:
            ids = bot.send(verdict.text)
            told, why = True, f"отправлено в чат {bot.chat_id}"
        except telegram.TelegramError as error:
            why = f"не отправилось: {error}"
        log(f"   {why}")

    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if told:
        health.write_journal(RUNS, health.remember(journal, verdict, today, when, ids))
    else:
        # Не сказали — не записываем, что сказали. Иначе тревога считается
        # доложенной, повтор не придёт, и радар останется молча слепым.
        health.write_journal(
            RUNS, health.remember(journal, health.Verdict("норма", "", verdict.fails,
                                                          verdict.last_ok),
                                  today, when, []))
    return {"вывод": verdict.kind, "провалов подряд": len(verdict.fails),
            "сказано": told, "почему": why}


# ─────────────────────────── настройки расписания ─────────────────────────────

def launcher() -> str:
    """Чем запускать по расписанию: pythonw.exe, если он рядом с python.exe.

    Разница одна и вся про человека: python.exe раз в сутки открывает чёрное
    окно консоли поверх работы и закрывает его через три минуты. Первое, что
    делает владелец с такой задачей, — отключает её. pythonw.exe работает молча,
    а весь вывод и так лежит в work/logs/.
    """
    exe = Path(sys.executable)
    quiet = exe.with_name("pythonw.exe")
    return str(quiet if quiet.exists() else exe)


def plan(cfg: dict) -> dict:
    """Настройки для установщика задачи. Только латиница: JSON читает PowerShell."""
    hours, _, minutes = str(cfg["time_utc"]).partition(":")
    at_utc = datetime.combine(date.today(), clock(int(hours), int(minutes)),
                              timezone.utc)
    at_local = at_utc.astimezone()
    return {
        "task": str(cfg["task_name"]),
        "python": launcher(),
        "script": str(ROOT / "daily.py"),
        "workdir": str(ROOT),
        "time_utc": at_utc.strftime("%H:%M"),
        "time_local": at_local.strftime("%H:%M"),
        "utc_offset": at_local.strftime("%z"),
        "timeout_min": int(cfg["step_timeout_min"]),
        "digest_weekday": int(cfg["digest_weekday"]),
    }


# ─────────────────────────── запуск ───────────────────────────────────────────

def main() -> int:
    console.setup()
    ap = argparse.ArgumentParser(description="Ежедневный прогон радара")
    ap.add_argument("--date", help="считать сегодняшним этот день (для проверки)")
    ap.add_argument("--dry-run", action="store_true",
                    help="прогнать цепочку вхолостую: ничего не сохранять и не слать")
    ap.add_argument("--no-digest", action="store_true",
                    help="не трогать недельную сводку")
    ap.add_argument("--no-health", action="store_true",
                    help="не проверять здоровье сбора")
    ap.add_argument("--health", action="store_true",
                    help="только проверить здоровье сбора и выйти")
    ap.add_argument("--plan", action="store_true",
                    help="показать настройки расписания в JSON и выйти")
    ap.add_argument("--quiet", action="store_true",
                    help="писать только в журнал, не на экран")
    args = ap.parse_args()

    cfg = load_config()
    today = date.fromisoformat(args.date) if args.date else date.today()

    if args.plan:
        print(json.dumps(plan(cfg), ensure_ascii=True))
        return 0

    log = Log(today.isoformat(), args.quiet)
    started = datetime.now(timezone.utc)
    log("")
    log("═" * 72)
    log(f"{started.astimezone().strftime('%Y-%m-%d %H:%M:%S %z')} · "
        + ("проверка здоровья" if args.health else "ежедневный прогон")
        + (" · холостой" if args.dry_run else ""))
    log("═" * 72)

    if args.health:
        state = health_pass(cfg, today, log, args.dry_run)
        log.close()
        return 0 if state["вывод"] in ("норма", "точка отсчёта", "починилось") else 1

    timeout = float(cfg["step_timeout_min"]) * 60
    if not take_lock(timeout):
        log("Прогон уже идёт (замок work/runs/daily.lock свежий). Второй не запускаю: "
            "два обхода сразу — это вдвое чаще стучаться в чужие сайты.")
        log.close()
        return 1

    steps, failed = [], False
    try:
        dry = ["--dry-run"] if args.dry_run else []

        step = run_step("сбор", "collect.py", dry, log, timeout)
        steps.append(step)
        # Единственный шаг, после которого цепочка может остановиться: без
        # снимков разбирать нечего, а сообщение «изменений нет» после несбора —
        # это неправда, которую радару говорить нельзя.
        if step["код"] not in (0, 1):
            failed = True
            log("")
            log("Сбор не отработал — разбор и уведомления пропущены.")
        else:
            when = ["--date", today.isoformat()]
            step = run_step("разбор", "detect.py", when + dry, log, timeout)
            steps.append(step)
            failed = failed or step["код"] != 0

            step = run_step("уведомления", "notify.py", when + dry, log, timeout)
            steps.append(step)
            failed = failed or step["код"] == 1

            if not args.no_digest:
                day, why = digest_plan(today, cfg)
                log("")
                log(f"── недельная сводка: {why}")
                if day is not None:
                    step = run_step("сводка", "notify.py",
                                    ["--digest", "--date", day.isoformat()] + dry,
                                    log, timeout)
                    steps.append(step)
                    failed = failed or step["код"] == 1
    finally:
        drop_lock()

    state = {"вывод": "не проверяли"}
    if not args.no_health:
        state = health_pass(cfg, today, log, args.dry_run)
    if state["вывод"] in ("тревога", "повтор"):
        failed = True

    gone = 0 if args.dry_run else prune_logs(today, cfg["keep_log_days"])
    spent = round((datetime.now(timezone.utc) - started).total_seconds(), 1)

    log("")
    log(f"Прогон закончен за {spent} с. Шагов: {len(steps)}, "
        f"с ненулевым кодом: {sum(1 for s in steps if s['код'] != 0)}.")
    if gone:
        log(f"Вычищено журналов старше {cfg['keep_log_days']} дней: {gone}.")
    log(f"Журнал прогона: work/logs/{today.isoformat()}.log")

    if not args.dry_run:
        journal = health.read_journal(RUNS)
        days = journal.setdefault("дни", {})
        days[today.isoformat()] = {
            "начат": started.isoformat(timespec="seconds"),
            "секунд": spent,
            "шаги": {s["шаг"]: s["код"] for s in steps},
            "здоровье": state["вывод"],
        }
        for old in sorted(days)[:-60]:
            days.pop(old)
        health.write_journal(RUNS, journal)

    log.close()

    # Коды возврата — то единственное, что видно в планировщике:
    #   0 — прогон прошёл;
    #   1 — шаг упал или сбор не идёт второй день подряд;
    #   2 — тревогу отправить не удалось, радар слеп и молчит об этом.
    if state["вывод"] in ("тревога", "повтор") and not state.get("сказано"):
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
