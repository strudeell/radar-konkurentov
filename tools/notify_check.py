#!/usr/bin/env python3
"""Проверка классификатора: что радар сочтёт срочным, а что оставит до сводки.

Зачем это отдельной программой. Правила срочности проверить на настоящих
изменениях нельзя — их просто нет: за 19.08.2026 у девятнадцати конкурентов
нашлось ровно одно изменение. Ждать, пока кто-нибудь поменяет цену, чтобы
узнать, работает ли главное правило системы, — плохой план: узнаем мы это в тот
день, когда сообщение не придёт.

Поэтому изменения делаются искусственно, но не из воздуха: берётся **настоящий
снимок настоящей страницы**, и в нём меняется ровно одна вещь — цена, тарифный
блок, заголовок первого экрана. Дальше правка проходит через тот же детектор
Фазы 3 и тот же классификатор Фазы 4, которыми работает боевой радар. Проверка
отвечает на вопрос «что человек получит, если конкурент сделает вот так».

Каждый случай объявляет, чего от него ждут. Не совпало — программа возвращает
ненулевой код: это регрессионная проверка, а не демонстрация.

Запуск:

    python tools/notify_check.py             прогнать все случаи
    python tools/notify_check.py --verbose   показать сообщения целиком
"""

import argparse
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import yaml  # noqa: E402

import classify  # noqa: E402
import detect  # noqa: E402
import diffing  # noqa: E402

SNAPSHOTS = ROOT / "snapshots"

OLD_DAY, NEW_DAY = "2026-08-18", "2026-08-19"

# Случаи проверки. Каждый — настоящая страница и одна правка в ней.
CASES = [
    {
        "name": "конкурент поднял цену тарифа",
        "domain": "salesai.ru", "page": "pricing",
        "do": [("замена", "49 000", "54 000", 0)],
        "expect": classify.CRITICAL, "rule": "цена изменилась",
    },
    {
        "name": "конкурент убрал тарифный блок",
        "domain": "imot.io", "page": "pricing",
        "do": [("вырезать", "Малый бизнес", 7)],
        "expect": classify.CRITICAL, "rule": "число исчезло",
    },
    {
        "name": "конкурент переписал обещание на первом экране",
        "domain": "rechka.ai", "page": "home",
        "do": [("замена",
                "Увеличьте продажи на 30% за счёт быстрой и точной ИИ-аналитики звонков",
                "Речевая аналитика для отдела продаж: находим потерянные сделки", 1)],
        "expect": classify.CRITICAL, "rule": "первый экран",
    },
    {
        "name": "на странице появилась гарантия результата",
        "domain": "rechka.ai", "page": "home",
        "do": [("вставить", 150,
                ["Гарантируем результат: не вырастет конверсия за три месяца — "
                 "вернём деньги."])],
        "expect": classify.CRITICAL, "rule": "гарантия результата",
    },
    {
        "name": "конкурент заявил анализ переписок и видеовстреч",
        "domain": "getcalls.ru", "page": "home",
        "do": [("вставить", 60,
                ["Теперь анализируем не только звонки: переписки в мессенджерах, "
                 "чаты на сайте и видеовстречи."])],
        "expect": classify.CRITICAL, "rule": "новые каналы анализа",
    },
    {
        "name": "конкурент объявил новую возможность продукта",
        "domain": "qolio.ru", "page": "home",
        "do": [("вставить", 40,
                ["Теперь можно выгружать отчёты по каждому менеджеру в Excel "
                 "одним нажатием."])],
        "expect": classify.CRITICAL, "rule": "новая возможность продукта",
    },
    {
        "name": "у конкурента вышла новая статья в блоге",
        "domain": "bewise.ai", "page": "blog",
        "do": [("вставить", 30,
                ["Как отдел продаж перестал терять сделки на этапе согласования",
                 "Разбираем на примере производственной компании, где узкое место "
                 "оказалось не в менеджерах, а в передаче заявки между отделами."])],
        "expect": classify.NORMAL, "rule": None,
    },
    {
        "name": "конкурент поправил формулировку в тексте",
        "domain": "bewise.ai", "page": "blog",
        "do": [("замена", "кому нужна помощь", "кому нужна поддержка", 1)],
        "expect": classify.NORMAL, "rule": None,
    },
    {
        "name": "конкурент переиндексировал весь прайс",
        "domain": "roistat.com", "page": "pricing",
        "do": [("+1 к числам", 60)],
        "expect": classify.CRITICAL, "rule": "наводнение чисел",
    },
]


def latest_snapshot(domain: str, page: str) -> Path | None:
    folder = SNAPSHOTS / domain / page
    files = sorted(folder.glob("*.txt")) if folder.exists() else []
    return files[-1] if files else None


def mutate(text: str, steps: list) -> str:
    """Сделать в снимке ровно то, что описано в случае."""
    lines = text.split("\n")
    for step in steps:
        action = step[0]

        if action == "замена":
            _, old, new, count = step
            joined = "\n".join(lines)
            if old not in joined:
                return ""
            joined = joined.replace(old, new) if count == 0 \
                else joined.replace(old, new, count)
            lines = joined.split("\n")

        elif action == "вырезать":
            _, marker, how_many = step
            if marker not in lines:
                return ""
            start = lines.index(marker)
            lines = lines[:start] + lines[start + how_many:]

        elif action == "вставить":
            _, where, added = step
            where = min(where, len(lines))
            lines = lines[:where] + list(added) + lines[where:]

        elif action == "+1 к числам":
            _, how_many = step
            done = 0
            for index, line in enumerate(lines):
                if done >= how_many:
                    break
                changed = re.sub(r"\b(\d{1,4})\b",
                                 lambda m: str(int(m.group(1)) + 1), line, count=1)
                if changed != line:
                    lines[index] = changed
                    done += 1
            if done < how_many:
                return ""

    return "\n".join(lines)


def run_case(case: dict, cfg: dict, rules: dict, index: dict, folder: Path) -> dict:
    """Прогнать один случай через настоящие детектор и классификатор."""
    base = latest_snapshot(case["domain"], case["page"])
    if base is None:
        return {"case": case, "skip": "снимка этой страницы нет"}

    old_text = base.read_text(encoding="utf-8")
    new_text = mutate(old_text, case["do"])
    if not new_text or new_text == old_text:
        return {"case": case, "skip": "на странице не нашлось того, что правим"}

    pair = folder / case["domain"] / case["page"]
    pair.mkdir(parents=True, exist_ok=True)
    old_file = pair / f"{OLD_DAY}.txt"
    new_file = pair / f"{NEW_DAY}.txt"
    old_file.write_text(old_text, encoding="utf-8", newline="\n")
    new_file.write_text(new_text, encoding="utf-8", newline="\n")

    meta = index.get((case["domain"], case["page"]), {})
    item = detect.examine(case["domain"], case["page"], old_file, new_file, meta, cfg)
    verdict = classify.judge(item, rules,
                             old_lines=diffing.split_lines(old_text),
                             new_lines=diffing.split_lines(new_text))

    ok = verdict.label == case["expect"]
    if case["rule"] and case["rule"] not in verdict.rules:
        ok = False
    return {"case": case, "item": item, "verdict": verdict, "ok": ok}


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка правил срочности радара")
    ap.add_argument("--verbose", action="store_true",
                    help="показать все улики и строки дельты")
    args = ap.parse_args()

    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    cfg = {**detect.DEFAULTS, **(config.get("detect") or {})}
    sources = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8")) or {}
    index = detect.source_index(sources)
    rules = classify.load_rules(ROOT / "rules.yaml")

    print("Проверка на настоящих снимках: в каждый внесена одна правка.\n"
          f"Порог детектора {cfg['min_changed_chars']} символов, "
          f"правил в словаре {len(rules['keywords'])}.\n")

    results = []
    with tempfile.TemporaryDirectory(prefix="radar-check-") as tmp:
        for case in CASES:
            results.append(run_case(case, cfg, rules, index, Path(tmp)))

    failed = 0
    for result in results:
        case = result["case"]
        head = f"{case['name']} ({case['domain']} · {case['page']})"
        if "skip" in result:
            print(f"  пропущено   {head}: {result['skip']}")
            failed += 1
            continue

        item, verdict = result["item"], result["verdict"]
        mark = "как ожидали" if result["ok"] else "НЕ СОВПАЛО"
        if not result["ok"]:
            failed += 1
        print(f"  {mark:<11} {head}")
        print(f"               детектор: {item['статус']}, "
              f"затронуто {item['разница']['затронуто символов']} символов")
        print(f"               классификатор: {verdict.label}"
              + (f" — {'; '.join(verdict.rules)}" if verdict.rules else ""))
        for reason in verdict.reasons:
            print(f"               • {reason}")
        shown = verdict.lines if args.verbose else verdict.lines[:2]
        for line in shown:
            print(f"                 {line[:160]}")
        if not verdict.critical and item["в дайджест"]:
            print("               → уйдёт в недельную сводку")
        elif not verdict.critical:
            print("               → ниже порога, человека не побеспокоит")
        else:
            fits = "" if item["в дайджест"] else " (правка мелкая, но по смыслу срочная)"
            print(f"               → уйдёт сразу{fits}")
        print()

    print(f"Случаев: {len(results)}, разошлось с ожиданием: {failed}.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
