"""Фаза 0, шаг A: главные страницы 19 конкурентов + robots.txt.

Что выясняем: отвечает ли домен вообще, режет ли по User-Agent, куда редиректит,
сколько видимого текста отдаёт без JS, и какие ссылки есть на главной
(из них на шаге B достаём тарифы / интеграции / блог / соцсети).
"""

import csv
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe

ROOT = Path(__file__).resolve().parent.parent

# CSV лежит в папке контекста, имя папки — с кириллицей, поэтому ищем glob-ом
CSV = next(ROOT.parent.glob("*/data/konkurenty.csv"))


def load_competitors():
    with CSV.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main():
    comps = load_competitors()
    print(f"competitors in csv: {len(comps)}")
    results = []

    for i, c in enumerate(comps, 1):
        domain = c["domain"]
        # для главной берём корень домена, даже если в CSV указан глубокий URL
        parsed = urlparse(c["site_url"])
        home = f"{parsed.scheme}://{parsed.netloc}/"

        print(f"[{i}/{len(comps)}] {domain} ... ", end="", flush=True)
        r = probe.fetch_with_fallback(home)

        vis = probe.visible_text(r["text"]) if r["text"] else ""
        found_links = probe.links(r["text"], r["final_url"] or home) if r["text"] else []
        if r["text"]:
            probe.save_raw(f"home__{domain}", r["text"])

        # robots.txt отдельным запросом
        rob = probe.fetch(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
        robots_ok = rob["status"] == 200 and "html" not in rob["content_type"].lower()

        rec = {
            "group": c["group"],
            "name": c["name"],
            "domain": domain,
            "csv_url": c["site_url"],
            "home_url": home,
            "status": r["status"],
            "bot_ua_status": r.get("bot_ua_status"),
            "ua_fallback_needed": r.get("ua_fallback_needed", False),
            "final_url": r["final_url"],
            "redirected": r["redirected"],
            "bytes": r["bytes"],
            "visible_chars": len(vis),
            "error": r["error"],
            "robots_status": rob["status"],
            "robots_text": rob["text"][:4000] if robots_ok else None,
            "links": [{"href": h, "text": t} for h, t in found_links],
            "link_count": len(found_links),
        }
        results.append(rec)
        print(
            f"status={r['status']} bot_ua={r.get('bot_ua_status', r['status'])} "
            f"vis={len(vis)} links={len(found_links)} robots={rob['status']}"
        )

    probe.dump_json("phase0-stage-a", results)
    print("\nsaved -> radar/data/phase0-stage-a.json")


if __name__ == "__main__":
    main()
