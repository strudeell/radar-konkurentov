"""Фаза 0, шаг B3: пере-выбор страниц по ссылкам с главной.

Шаг B брал первую ссылку, попавшую под шаблон, и промахивался: у Roistat
«блогом» оказалась страница фичи /features/mediaplan (сработало слово media),
у Mango — отдельная статья журнала вместо самого журнала.

Здесь кандидаты ранжируются: нужен раздел-индекс (/blog/), а не материал
внутри раздела (/blog/2026/kak-my-...). Индекс меняется при каждой публикации —
именно это и есть сигнал.

Плюс распознаём страницы-обманки: капчу и JS-заглушку. Формально это 200,
но контента там нет, и в config/sources.yaml им не место.
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe

ROOT = Path(__file__).resolve().parent.parent

# точное совпадение последнего сегмента пути — признак раздела-индекса
INDEX_SEG = {
    "pricing": {"price", "pricing", "prices", "tariffs", "tarify", "tarifi", "ceny",
                "cost", "plans", "tariff", "annual_price", "payment"},
    "integrations": {"integrations", "integration", "integracii", "integratsii",
                     "connection", "connect", "partners"},
    "blog": {"blog", "news", "novosti", "articles", "stati", "media", "journal",
             "cases", "keys", "press", "publications"},
}
# то, что заведомо не раздел: материал внутри раздела или чужая тема
NEGATIVE = {
    "pricing": re.compile(r"/for_free|/free/|/crm/|/promo|/oplata-|/success", re.I),
    "integrations": re.compile(r"/partners/\w|/support/", re.I),
    "blog": re.compile(r"/features?/|/watch|/case_|/solutions?/|/product", re.I),
}

CAPTCHA_RE = re.compile(r"вы не робот|captcha|подтвердите, что запросы", re.I)
JS_STUB_RE = re.compile(r"включите javascript|enable javascript|requires javascript", re.I)


def score(url: str, kind: str) -> int:
    p = urlparse(url).path.rstrip("/")
    segs = [s for s in p.split("/") if s]
    if not segs:
        return -999
    if NEGATIVE[kind].search(p + "/"):
        return -999
    s = 0
    last = segs[-1].lower()
    if last in INDEX_SEG[kind]:
        s += 100
    elif any(k in last for k in INDEX_SEG[kind]):
        s += 40
    # раздел-индекс лежит неглубоко
    s -= 12 * (len(segs) - 1)
    s -= len(p) // 20
    # языковой префикс (/ru/blog) не считаем «глубиной»
    if segs[0].lower() in ("ru", "en", "rus"):
        s += 12
    return s


def check_page(url: str) -> dict:
    r = probe.fetch_probe(url)
    vis = probe.visible_text(r["text"]) if r["text"] else ""
    verdict = "ok"
    if r["status"] != 200:
        verdict = f"http {r['status']}"
    elif CAPTCHA_RE.search(vis[:1500]):
        verdict = "капча (бот-защита)"
    elif JS_STUB_RE.search(vis[:1500]) or len(vis) < 500:
        verdict = f"пусто без JS ({len(vis)} симв.)"
    return {"url": url, "status": r["status"], "final_url": r["final_url"],
            "visible_chars": len(vis), "verdict": verdict, "html": r["text"]}


def main():
    stage_a = {r["domain"]: r for r in
               json.load((ROOT / "data" / "phase0-stage-a.json").open(encoding="utf-8"))}
    stage_c = json.load((ROOT / "data" / "phase0-stage-c.json").open(encoding="utf-8"))

    for rec in stage_c:
        dom = rec["domain"]
        a = stage_a[dom]
        if a["status"] is None:
            continue
        host = urlparse(a["home_url"]).netloc.replace("www.", "").lower()

        for kind in ("pricing", "integrations", "blog"):
            cands = []
            for l in a["links"]:
                u = urlparse(l["href"])
                if u.netloc.replace("www.", "").lower() != host:
                    continue
                sc = score(l["href"].split("#")[0], kind)
                if sc > -999:
                    seg_hit = any(k in urlparse(l["href"]).path.lower()
                                  for k in INDEX_SEG[kind])
                    if seg_hit:
                        cands.append((sc, l["href"].split("#")[0]))
            if not cands:
                continue
            best = sorted(set(cands), reverse=True)[0][1]

            cur = (rec["pages"].get(kind) or {}).get("url")
            if best == cur and (rec["pages"].get(kind) or {}).get("valid"):
                continue

            res = check_page(best)
            print(f"{dom:22} {kind:13} {res['verdict']:22} {best}", flush=True)
            if res["verdict"] == "ok":
                probe.save_raw(f"{kind}__{dom}", res["html"])
                rec["pages"][kind] = {
                    "url": best, "final_url": res["final_url"], "status": res["status"],
                    "visible_chars": res["visible_chars"], "ua_fallback_needed": False,
                    "robots_allowed": True, "source": "link-repick", "valid": True,
                }
            elif rec["pages"].get(kind) and not rec["pages"][kind].get("valid"):
                rec["pages"][kind]["repick_tried"] = best
                rec["pages"][kind]["repick_verdict"] = res["verdict"]

        # перепроверяем уже принятые страницы на капчу/пустоту
        for kind in ("pricing", "integrations", "blog"):
            p = rec["pages"].get(kind)
            if not p or not p.get("valid"):
                continue
            f = ROOT / "data" / "_raw" / f"{kind}__{dom}.html"
            if not f.exists():
                continue
            vis = probe.visible_text(f.read_text(encoding="utf-8"))
            if CAPTCHA_RE.search(vis[:1500]):
                p["valid"] = False
                p["why"] = "капча вместо страницы — нужен браузер или ручная проверка"
                print(f"{dom:22} {kind:13} ОТБРОШЕНА: капча", flush=True)
            elif len(vis) < 500:
                p["valid"] = False
                p["why"] = f"пустая без JS ({len(vis)} симв.)"
                print(f"{dom:22} {kind:13} ОТБРОШЕНА: пусто без JS", flush=True)

    probe.dump_json("phase0-stage-c", stage_c)
    print("\nобновлено -> radar/data/phase0-stage-c.json")


if __name__ == "__main__":
    main()
