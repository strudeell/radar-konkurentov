#!/usr/bin/env python3
"""Замер фонового шума: сколько «меняется» страница, на которой ничего не меняли.

Зачем это нужно. Порог в 120 символов взят из технического плана, и до сих пор
это была разумная догадка, а не измеренная величина. Проверить её можно только
одним способом — взять две съёмки одной и той же страницы, между которыми
конкурент заведомо ничего не делал, и посмотреть, сколько символов детектор
насчитает изменившимися. Всё, что он там найдёт, — шум по определению.

Откуда берутся такие пары. В Фазе 0 разведка шла в два прохода: сначала поиск
страниц (data/_raw/<вид>__<домен>.html), потом проверка выбранных адресов
(data/_raw/final__<вид>__<домен>.html). Между проходами прошло от пятнадцати
минут до получаса. Там, где оба прохода ходили по одному и тому же адресу,
получилась готовая пара «одна страница, снятая дважды за день».

Адреса сверяются по data/phase0-*.json: во втором проходе часть страниц была
переназначена на другие адреса, и такие пары в замер не берутся — это разные
страницы, разница между ними настоящая.

Запуск:

    python tools/noise_check.py            замер по парам из Фазы 0
    python tools/noise_check.py --verbose  показать сами строки, которые шумят
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import diffing  # noqa: E402
import normalize  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "_raw"
DATA = ROOT / "data"


def _url(info: dict | None) -> str | None:
    if not info:
        return None
    return info.get("final_url") or info.get("url")


def pairs() -> list[tuple[str, str, Path, Path]]:
    """Пары «один адрес, две съёмки» — домен, вид страницы, первый файл, второй."""
    stage_b = json.loads((DATA / "phase0-stage-b.json").read_text(encoding="utf-8"))
    final = json.loads((DATA / "phase0-final-pages.json").read_text(encoding="utf-8"))

    out = []
    for record in stage_b:
        domain = record["domain"]
        known = {"home": record.get("home"), **(record.get("pages") or {})}
        for kind, info in known.items():
            first = RAW / f"{kind}__{domain}.html"
            second = RAW / f"final__{kind}__{domain}.html"
            if not (first.exists() and second.exists()):
                continue
            if _url(info) != _url((final.get(domain) or {}).get(kind)):
                continue          # во втором проходе адрес переназначили
            out.append((domain, kind, first, second))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Замер фонового шума детектора")
    ap.add_argument("--verbose", action="store_true",
                    help="показать строки, которые детектор счёл изменившимися")
    args = ap.parse_args()

    if not RAW.exists():
        print("Нет папки data/_raw — замер делать не на чем.")
        print("Она восстанавливается прогоном tools/stage_*.py.")
        return 1

    found = pairs()
    print(f"Пар «один адрес, две съёмки за день»: {len(found)}\n")

    noisy = []
    for domain, kind, first, second in sorted(found):
        old = normalize.to_snapshot(first.read_text(encoding="utf-8", errors="replace"),
                                    domain, kind=kind)
        new = normalize.to_snapshot(second.read_text(encoding="utf-8", errors="replace"),
                                    domain, kind=kind)
        delta = diffing.compare(old, new)
        if delta.empty and not delta.moved:
            continue
        noisy.append((delta.changed_chars, domain, kind, delta))

    for chars, domain, kind, delta in sorted(noisy, reverse=True):
        print(f"  {chars:>6} символов  {domain}/{kind}"
              f"   (переехало строк: {len(delta.moved)}, "
              f"отсеяно шумодавом: {len(delta.ignored)})")
        if args.verbose:
            for line in delta.added[:5]:
                print("           +", line[:120])
            for line in delta.removed[:5]:
                print("           −", line[:120])

    quiet = len(found) - len(noisy)
    print(f"\nСовпало полностью: {quiet} из {len(found)}")
    if noisy:
        worst = max(chars for chars, *_ in noisy)
        print(f"Больше всего шума на одной странице: {worst} символов")
        print("Порог в config.yaml (detect.min_changed_chars) должен стоять выше "
              "этого числа,\nиначе фон будет попадать в дайджест как изменение.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
