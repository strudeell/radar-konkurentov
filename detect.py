#!/usr/bin/env python3
"""Детектор изменений — Фаза 3.

Сборщик Фазы 2 отвечает только «текст тот же» или «текст другой». Эта программа
отвечает на вопрос «что именно изменилось»: какие строки появились, какие
исчезли, сколько символов затронуто и — отдельно, самым ценным пунктом — какие
числа на страницах тарифов стали другими.

Результат ложится в два места:

    diffs/<домен>/<страница>/<ГГГГ-ММ-ДД>.json   разница по одному источнику
    diffs/<ГГГГ-ММ-ДД>.json                       сводка дня

Диффы не чистятся никогда. Полные снимки старше 90 дней сборщик удаляет — они
тяжёлые и спустя квартал бесполезны, — а диффы весят килобайты, и в них вся
история изменений конкурента. Через год снимков за май не будет, а разница,
случившаяся в мае, останется.

Три правила, которые определяют поведение детектора.

**Порог отсекает мелочь, но не числа.** Изменение попадает в дайджест, если
затронуто больше 120 символов видимого текста. Правка цены задевает восемь:
«49 000 ₽» → «54 000 ₽». Общий порог убил бы главный сигнал системы, поэтому
любое изменение числа на странице тарифов идёт в отчёт независимо от объёма.

**Перестановка блоков — не изменение.** Строка, ушедшая в одном месте и дословно
появившаяся в другом, считается переехавшей и в объём не входит. Иначе новая
статья в блоге, вытолкнувшая остальные вниз, выглядела бы как переписанная
страница. Подробности в tools/diffing.py.

**«Не проверяли» и «проверили, ничего не изменилось» — разные вещи.** Источник,
который сегодня не собрался, попадает в сводку отдельным списком, а не молча
приравнивается к неизменившемуся. Сводка берёт это из отчёта сборщика
runs/<дата>.json.

Запуск:

    python detect.py                     разбор за сегодня
    python detect.py --date 2026-08-19   разбор за конкретный день
    python detect.py --all               пересчитать всю историю снимков
    python detect.py --only mango-office.ru   только один домен (можно повторять)
    python detect.py --full              печатать все строки, а не первые несколько
    python detect.py --dry-run           показать, но ничего не записывать
"""

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools"))

import yaml  # noqa: E402

import diffing  # noqa: E402
import prices  # noqa: E402

SNAPSHOTS = ROOT / "snapshots"
DIFFS = ROOT / "diffs"
RUNS = ROOT / "runs"

# Значения по умолчанию. Человек меняет их в config.yaml, раздел detect.
DEFAULTS = {
    # Порог из технического плана. Меньше — не повод беспокоить человека.
    # Калибруется на живых данных в Фазе 6, а не выдумывается заранее.
    "min_changed_chars": 120,
    # Числа на страницах тарифов идут в отчёт при любом объёме изменения.
    "numbers_ignore_threshold": True,
    # Сколько строк дельты показывать в сводке и на экране.
    "sample_lines": 8,
}

S_CHANGED = "изменилось"
S_MINOR = "мелочь"
S_SHUFFLE = "только перестановка"
S_BASELINE = "точка отсчёта"
S_SAME = "без изменений"
S_UNCHECKED = "не проверено"
S_BY_HAND = "смотрим глазами"

# Статусы сборщика, означающие «сегодня не получилось, данных за день нет».
# Это сбой, и в Фазе 5 он должен доходить до человека.
COLLECT_FAILED = {"ошибка", "подозрение", "закрыто сайтом"}

# А это не сбой: источник исключён из обхода сознательно (мёртвый сайт,
# защита от роботов). Смешивать их нельзя — иначе здоровье системы в Фазе 5
# будет каждый день показывать два отказа, которых на самом деле нет.
COLLECT_SKIPPED = "пропущено"


def load_yaml(name: str) -> dict:
    path = ROOT / name
    if not path.exists():
        sys.exit(f"Не найден {name} рядом с detect.py.")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def competitor_names(sources: dict) -> dict:
    """Домен → имя игрока. Нужно и для тех, кого мы не собираем."""
    return {comp["domain"]: comp["name"] for comp in sources.get("competitors", [])}


def source_index(sources: dict) -> dict:
    """Справочник «домен + страница» → что мы про этот источник знаем."""
    index = {}
    for comp in sources.get("competitors", []):
        domain = comp["domain"]
        for page in comp.get("pages", []):
            index[(domain, page["kind"])] = {
                "конкурент": comp["name"],
                "адрес": page["url"],
                "важность": page.get("priority", "normal"),
                "числа": bool(page.get("extract_numbers")),
            }
        for ch in comp.get("channels", []):
            index[(domain, f"{ch['net']}-{ch['id']}")] = {
                "конкурент": comp["name"],
                "адрес": ch.get("url"),
                "важность": "normal",
                "числа": False,
            }
    return index


def snapshot_pairs(only: list[str], target: str | None, every: bool) -> list[tuple]:
    """Пары снимков, которые надо сравнить: (домен, страница, вчера, сегодня).

    В обычном режиме берётся только свежая пара — та, у которой новый снимок
    снят в разбираемый день. Иначе детектор каждый день пересказывал бы одно и
    то же изменение недельной давности: сборщик не пишет новый файл, пока текст
    не поменялся, и последняя пара у редко меняющейся страницы висит месяцами.
    """
    out = []
    for folder in sorted(SNAPSHOTS.glob("*/*")):
        if not folder.is_dir():
            continue
        domain, page = folder.parent.name, folder.name
        if only and domain.lower() not in only:
            continue
        files = sorted(folder.glob("*.txt"))
        if len(files) < 2:
            continue
        if every:
            out.extend((domain, page, a, b) for a, b in zip(files, files[1:]))
        elif target is None or files[-1].stem == target:
            out.append((domain, page, files[-2], files[-1]))
    return out


def collect_report(day: str) -> dict:
    """Отчёт сборщика за этот день. Из него берётся «что не проверено»."""
    path = RUNS / f"{day}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def examine(domain: str, page: str, old_file: Path, new_file: Path,
            meta: dict, cfg: dict) -> dict:
    """Разобрать одну пару снимков."""
    old_text = old_file.read_text(encoding="utf-8")
    new_text = new_file.read_text(encoding="utf-8")
    delta = diffing.compare(old_text, new_text)

    gap = (date.fromisoformat(new_file.stem) - date.fromisoformat(old_file.stem)).days
    item = {
        "конкурент": meta.get("конкурент", domain),
        "домен": domain,
        "страница": page,
        "адрес": meta.get("адрес"),
        "важность": meta.get("важность", "normal"),
        "дата": new_file.stem,
        "сравнили со снимком": old_file.stem,
        "разрыв в днях": gap,
        "разница": delta.to_dict(),
    }

    numbers = None
    if meta.get("числа"):
        numbers = prices.compare(diffing.split_lines(old_text),
                                 diffing.split_lines(new_text))
        item["числа"] = prices.summary(numbers)

    threshold = int(cfg["min_changed_chars"])
    moved_numbers = bool(numbers and (numbers["изменилось"] or numbers["появилось"]
                                      or numbers["исчезло"]))

    if delta.empty:
        item["статус"] = S_SHUFFLE
        item["почему"] = ("текст тот же, изменился порядок строк или ушли только "
                          "метки шумодава")
    elif delta.changed_chars >= threshold:
        item["статус"] = S_CHANGED
        item["почему"] = f"затронуто {delta.changed_chars} символов при пороге {threshold}"
    elif moved_numbers and cfg["numbers_ignore_threshold"]:
        item["статус"] = S_CHANGED
        item["почему"] = (f"затронуто всего {delta.changed_chars} символов, но это "
                          "числа на коммерческой странице — порог к ним не применяется")
    else:
        item["статус"] = S_MINOR
        item["почему"] = f"затронуто {delta.changed_chars} символов, порог {threshold}"

    item["в дайджест"] = item["статус"] == S_CHANGED
    item["кратко"] = _headline(delta, numbers, int(cfg["sample_lines"]))
    return item


def _headline(delta: diffing.Delta, numbers: dict | None, sample: int) -> list[str]:
    """Несколько строк, по которым человек поймёт суть, не открывая файл."""
    out = []
    for change in (numbers or {}).get("изменилось", []):
        out.append("₽ " + change.human())
    for gone in (numbers or {}).get("исчезло", [])[:sample]:
        out.append(f"₽ исчезло {gone.human()}  [{gone.context}]")
    for fresh in (numbers or {}).get("появилось", [])[:sample]:
        out.append(f"₽ появилось {fresh.human()}  [{fresh.context}]")
    for line in delta.added[:sample]:
        out.append("+ " + line)
    for line in delta.removed[:sample]:
        out.append("− " + line)
    return out


def day_summary(day: str, items: list[dict], cfg: dict, index: dict,
                names: dict) -> dict:
    """Сводка дня: что изменилось, что мелочь, а что сегодня не проверялось.

    Здесь же разводятся три разных вида тишины, которые легко перепутать и
    нельзя: «проверили, всё по-старому», «сегодня проверить не вышло» и
    «этот источник мы не собираем сознательно». Первое — измерение, второе —
    сбой, до которого в Фазе 5 должен дойти человек, третье — решение Фазы 0.
    """
    report = collect_report(day)
    checked = {(i["domain"], i["page"]): i for i in report.get("items", [])}
    looked_at = {(i["домен"], i["страница"]) for i in items}

    same, failed, baseline, by_hand = [], [], [], []
    alive: set[str] = set()
    for (domain, page), collected in sorted(checked.items()):
        who = index.get((domain, page), {}).get("конкурент") or names.get(domain, domain)
        line = f"{who} · {page}"
        status = collected["status"]

        if status == COLLECT_SKIPPED:
            by_hand.append(f"{who}: {collected['note']}")
            continue
        if status in COLLECT_FAILED:
            failed.append(f"{line}: {collected['note']}")
            continue

        alive.add(domain)
        if (domain, page) in looked_at:
            continue
        if status == "точка отсчёта":
            baseline.append(line)
        else:
            same.append(line)

    # «Ничего не менялось у …» — это тоже информация, и в дайджест она пойдёт
    # одной строкой. Но сказать так можно только про тех, кого сегодня
    # действительно проверили: мёртвый сайт в этот список попадать не должен.
    changed_domains = {i["домен"] for i in items if i["в дайджест"]}
    quiet = sorted({names.get(d, d) for d in alive - changed_domains})

    totals = {S_CHANGED: sum(1 for i in items if i["статус"] == S_CHANGED),
              S_MINOR: sum(1 for i in items if i["статус"] == S_MINOR),
              S_SHUFFLE: sum(1 for i in items if i["статус"] == S_SHUFFLE),
              S_SAME: len(same), S_BASELINE: len(baseline),
              S_UNCHECKED: len(failed), S_BY_HAND: len(by_hand)}

    return {
        "дата": day,
        "разобрано": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "порог символов": int(cfg["min_changed_chars"]),
        "итоги": totals,
        "в дайджест": [_short(i) for i in items if i["в дайджест"]],
        "ниже порога": [_short(i) for i in items if i["статус"] == S_MINOR],
        "только перестановка": [f"{i['конкурент']} · {i['страница']}"
                                for i in items if i["статус"] == S_SHUFFLE],
        "без изменений": same,
        "точка отсчёта": baseline,
        "не проверено": failed,
        "смотрим глазами": by_hand,
        "ничего не менялось у": quiet,
    }


def _short(item: dict) -> dict:
    return {
        "конкурент": item["конкурент"],
        "страница": item["страница"],
        "адрес": item["адрес"],
        "важность": item["важность"],
        "затронуто символов": item["разница"]["затронуто символов"],
        "сравнили со снимком": item["сравнили со снимком"],
        "почему": item["почему"],
        "кратко": item["кратко"],
        "дельта": f"diffs/{item['домен']}/{item['страница']}/{item['дата']}.json",
    }


def save(item: dict, dry_run: bool) -> None:
    if dry_run:
        return
    folder = DIFFS / item["домен"] / item["страница"]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{item['дата']}.json"
    path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Детектор изменений радара")
    ap.add_argument("--date", help="разбирать этот день (по умолчанию сегодня)")
    ap.add_argument("--all", action="store_true",
                    help="пересчитать все пары снимков за всю историю")
    ap.add_argument("--only", action="append", default=[],
                    help="только этот домен (ключ можно повторять)")
    ap.add_argument("--full", action="store_true",
                    help="печатать все строки дельты, а не первые несколько")
    ap.add_argument("--dry-run", action="store_true", help="ничего не записывать")
    args = ap.parse_args()

    config = load_yaml("config.yaml")
    cfg = {**DEFAULTS, **(config.get("detect") or {})}
    sources = load_yaml("sources.yaml")
    index = source_index(sources)
    names = competitor_names(sources)
    today = args.date or date.today().isoformat()
    only = [o.lower() for o in args.only]

    pairs = snapshot_pairs(only, None if args.all else today, args.all)
    started = datetime.now(timezone.utc)

    if args.all:
        print(f"Пересчёт всей истории: {len(pairs)} пар снимков.\n")
    else:
        print(f"Разбор за {today}. Порог {cfg['min_changed_chars']} символов.\n")
        if not pairs:
            print("  Сравнивать нечего: за этот день сборщик не записал ни одного\n"
                  "  нового снимка. Это не поломка — снимок пишется только тогда,\n"
                  "  когда текст страницы изменился.\n")

    items = []
    for domain, page, old_file, new_file in pairs:
        meta = index.get((domain, page), {})
        item = examine(domain, page, old_file, new_file, meta, cfg)
        items.append(item)
        save(item, args.dry_run)

        mark = {S_CHANGED: "ИЗМЕНЕНО", S_MINOR: "мелочь",
                S_SHUFFLE: "перестановка"}[item["статус"]]
        head = f"{item['конкурент']} · {page}"
        counts = (f"+{item['разница']['добавлено символов']} / "
                  f"−{item['разница']['удалено символов']} символов")
        gap = "" if item["разрыв в днях"] == 1 else \
            f", разрыв {item['разрыв в днях']} дн. (со снимком от {old_file.stem})"
        print(f"  {mark:<12} {head}: {counts}{gap}")
        shown = item["кратко"] if args.full else item["кратко"][:int(cfg["sample_lines"])]
        for line in shown:
            print(f"               {line[:150]}")
        hidden = len(item["кратко"]) - len(shown)
        if hidden > 0:
            print(f"               … и ещё {hidden} строк, целиком в файле дельты")

    if not args.all:
        summary = day_summary(today, items, cfg, index, names)
        if not args.dry_run:
            DIFFS.mkdir(parents=True, exist_ok=True)
            (DIFFS / f"{today}.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nГотово за {round((datetime.now(timezone.utc) - started).total_seconds(), 1)} с.")
        for status, number in summary["итоги"].items():
            if number:
                print(f"  {status}: {number}")
        if summary["не проверено"]:
            print("\nСегодня не проверено (это не «без изменений»):")
            for line in summary["не проверено"]:
                print("  •", line)
        if not args.dry_run:
            print(f"\nСводка дня: diffs/{today}.json")
    else:
        print(f"\nРазобрано пар: {len(items)}.")

    if args.dry_run:
        print("\nЭто был холостой разбор: ничего не записано.")

    # Ненулевой код возврата — сигнал для расписания Фазы 5: есть что показать.
    return 0


if __name__ == "__main__":
    sys.exit(main())
