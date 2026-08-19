#!/usr/bin/env python3
"""Точка отсчёта из страниц, сохранённых в Фазе 0.

Зачем. При разведке источников 18.08.2026 каждая страница была не только
проверена, но и сохранена целиком — 101 файл в data/_raw. Это готовый вчерашний
день. Если превратить их в снимки, первый же боевой прогон даст не «шестьдесят
точек отсчёта», а настоящее суточное сравнение: что изменилось у конкурентов
за сутки. Ждать неделю ради этого незачем.

Что делает. Берёт из data/_raw файлы вида final__<страница>__<домен>.html,
прогоняет через ту же нормализацию, что и ежедневный сбор, и кладёт результат
в snapshots/<домен>/<страница>/<дата сохранения файла>.txt.

Честность даты. Дата снимка берётся не с потолка и не сегодняшняя, а та, когда
файл был реально скачан. Иначе в истории появился бы снимок, датированный днём,
в который его никто не снимал.

Каналы соцсетей так восстановить нельзя: в Фазе 0 они не сохранялись. Их точка
отсчёта появится при первом обычном прогоне.

Запуск (делается один раз):

    python tools/seed_baseline.py            посмотреть, что получится
    python tools/seed_baseline.py --write    записать снимки
"""

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

import normalize  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "_raw"
SNAPSHOTS = ROOT / "snapshots"


def main() -> int:
    ap = argparse.ArgumentParser(description="Снимки-точки отсчёта из данных Фазы 0")
    ap.add_argument("--write", action="store_true", help="записать файлы")
    args = ap.parse_args()

    sources = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
    made = skipped = 0

    for comp in sources["competitors"]:
        if comp.get("status") != "ok":
            continue
        for page in comp.get("pages", []):
            raw = RAW / f"final__{page['kind']}__{comp['domain']}.html"
            if not raw.exists():
                print(f"  нет сохранённой страницы: {raw.name}")
                skipped += 1
                continue

            when = date.fromtimestamp(raw.stat().st_mtime).isoformat()
            folder = SNAPSHOTS / comp["domain"] / page["kind"]
            target = folder / f"{when}.txt"
            if target.exists():
                skipped += 1
                continue

            html = raw.read_text(encoding="utf-8", errors="replace")
            is_html = "<" in html[:200]
            text = normalize.to_snapshot(html, comp["domain"], is_html=is_html,
                                         kind=page["kind"])

            baseline = page.get("baseline_visible_chars")
            mark = ""
            if baseline:
                diff = len(text) - baseline
                mark = f"  (эталон Фазы 0: {baseline}, разница {diff:+d})"
            print(f"  {when}  {comp['domain']}/{page['kind']}: {len(text)} символов{mark}")

            if args.write:
                folder.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8", newline="\n")
            made += 1

    print(f"\nСнимков: {made}, пропущено: {skipped}")
    if not args.write:
        print("Это был просмотр. Записать: python tools/seed_baseline.py --write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
