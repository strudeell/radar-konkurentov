"""Фаза 0, шаг B: находим у каждого конкурента страницы тарифов, интеграций, блога
и реальные соцканалы.

Два источника кандидатов:
1. ссылки, собранные с главной на шаге A (приоритет — это реальные ссылки сайта);
2. типовые пути (/pricing, /tarify, ...) — для сайтов, где навигация на JS
   и ссылок в HTML нет.

Каждый кандидат проверяется живым запросом. Догадки без проверки не попадают
в результат — правило Фазы 0.
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe

ROOT = Path(__file__).resolve().parent.parent

KEYS = {
    "pricing": [
        r"/pricing", r"/price", r"/prices", r"/tariff", r"/tarif", r"/tary",
        r"/ceny", r"/cena", r"/cost", r"/stoimost", r"/plans", r"/payment",
    ],
    "integrations": [
        r"/integrat", r"/integrac", r"/integrations", r"/connect", r"/partners",
    ],
    "blog": [
        r"/blog", r"/news", r"/novosti", r"/articles", r"/stati", r"/media",
        r"/journal", r"/cases", r"/keys", r"/press",
    ],
}
TEXT_KEYS = {
    "pricing": ["цен", "тариф", "стоимост", "price", "pricing", "оплат"],
    "integrations": ["интеграц", "integration", "подключ"],
    "blog": ["блог", "новост", "стать", "blog", "news", "медиа", "кейс", "публикац"],
}
GUESS_PATHS = {
    "pricing": ["/pricing", "/pricing/", "/price", "/price/", "/prices/", "/tariffs/",
                "/tarify/", "/tarifi/", "/ceny/", "/plans/", "/cost/"],
    "integrations": ["/integrations", "/integrations/", "/integracii/", "/integration/"],
    "blog": ["/blog", "/blog/", "/news/", "/novosti/", "/articles/", "/media/"],
}

SOCIAL = {
    "vk": r"(?:^|\.)vk\.com/",
    "telegram": r"(?:^|\.)t\.me/",
    "youtube": r"(?:^|\.)(?:youtube\.com|youtu\.be)/",
    "dzen": r"(?:^|\.)(?:dzen\.ru|zen\.yandex\.ru)/",
    "vc": r"(?:^|\.)vc\.ru/",
    "habr": r"(?:^|\.)habr\.com/",
    "rutube": r"(?:^|\.)rutube\.ru/",
}


def classify(href: str, text: str) -> str | None:
    path = urlparse(href).path.lower()
    for kind, pats in KEYS.items():
        if any(re.search(p, path) for p in pats):
            return kind
    low = text.lower()
    for kind, words in TEXT_KEYS.items():
        if any(w in low for w in words) and len(text) < 40:
            return kind
    return None


def robots_allows(robots_text: str | None, url: str) -> bool | None:
    if not robots_text:
        return None
    rp = RobotFileParser()
    rp.parse(robots_text.splitlines())
    return rp.can_fetch(probe.UA_BOT, url) and rp.can_fetch("*", url)


def main():
    stage_a = json.load((ROOT / "data" / "phase0-stage-a.json").open(encoding="utf-8"))
    out = []

    for rec in stage_a:
        domain = rec["domain"]
        host = urlparse(rec["home_url"]).netloc

        # недоступный на шаге A домен не перебираем путями — это только
        # десятки бессмысленных запросов в стену
        if rec["status"] is None:
            print(f"\n=== {domain}: недоступен на шаге A — пропуск", flush=True)
            out.append({
                "domain": domain, "name": rec["name"], "group": rec["group"],
                "home": {"url": rec["home_url"], "status": None, "visible_chars": 0,
                         "ua_fallback_needed": False},
                "pages": {"pricing": None, "integrations": None, "blog": None},
                "socials": {}, "robots_status": rec["robots_status"],
                "unreachable_error": rec["error"],
            })
            continue

        print(f"\n=== {domain}", flush=True)

        # --- соцканалы с главной (реальные ссылки, не догадки) ---
        socials: dict[str, list[str]] = {}
        for l in rec["links"]:
            netloc = urlparse(l["href"]).netloc.lower()
            for net, pat in SOCIAL.items():
                if re.search(pat, netloc + "/"):
                    socials.setdefault(net, [])
                    if l["href"] not in socials[net]:
                        socials[net].append(l["href"])

        # --- кандидаты страниц из ссылок главной ---
        candidates: dict[str, list[str]] = {"pricing": [], "integrations": [], "blog": []}
        for l in rec["links"]:
            u = urlparse(l["href"])
            if u.netloc.lower().replace("www.", "") != host.lower().replace("www.", ""):
                continue
            kind = classify(l["href"], l["text"])
            if kind:
                clean = l["href"].split("#")[0]
                if clean not in candidates[kind]:
                    candidates[kind].append(clean)

        # --- если ссылок нет, пробуем типовые пути ---
        for kind, paths in GUESS_PATHS.items():
            if candidates[kind]:
                continue
            base = f"{urlparse(rec['home_url']).scheme}://{host}"
            candidates[kind] = [base + p for p in paths]

        pages = {}
        for kind in ("pricing", "integrations", "blog"):
            found = None
            for cand in candidates[kind][:5]:
                allowed = robots_allows(rec["robots_text"], cand)
                if allowed is False:
                    print(f"  {kind:13} SKIP (robots.txt запрещает) {cand}")
                    continue
                r = probe.fetch_probe(cand)
                vis = probe.visible_text(r["text"]) if r["text"] else ""
                ok = r["status"] == 200 and len(vis) > 200
                print(f"  {kind:13} {r['status']} vis={len(vis):<6} {cand}", flush=True)
                if ok:
                    fname = f"{kind}__{domain}".replace("/", "_")
                    probe.save_raw(fname, r["text"])
                    found = {
                        "url": cand,
                        "final_url": r["final_url"],
                        "status": r["status"],
                        "visible_chars": len(vis),
                        "ua_fallback_needed": r.get("ua_fallback_needed", False),
                        "robots_allowed": allowed,
                        "source": "link" if cand in candidates[kind][:5] else "guess",
                    }
                    break
            pages[kind] = found
            if not found:
                print(f"  {kind:13} -> НЕТ")

        out.append({
            "domain": domain,
            "name": rec["name"],
            "group": rec["group"],
            "home": {
                "url": rec["final_url"] or rec["home_url"],
                "status": rec["status"],
                "visible_chars": rec["visible_chars"],
                "ua_fallback_needed": rec["ua_fallback_needed"],
            },
            "pages": pages,
            "socials": socials,
            "robots_status": rec["robots_status"],
        })

    probe.dump_json("phase0-stage-b", out)
    print("\nsaved -> radar/data/phase0-stage-b.json")


if __name__ == "__main__":
    main()
