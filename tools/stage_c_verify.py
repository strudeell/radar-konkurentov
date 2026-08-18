"""Фаза 0, шаг C: валидация найденного и проверка соцканалов.

Три вещи, которые нельзя пропустить:
1. Soft-404. Сайт отдаёт 200 и главную страницу вместо запрошенной. Если это
   не отсечь, в sources.yaml попадёт «страница тарифов», которая на деле копия
   главной: она будет меняться синхронно с главной и давать двойной шум.
2. Telegram читаем только через публичную веб-версию t.me/s/<канал>.
   Личные аккаунты, боты и инвайт-ссылки так не читаются — их надо отсеять сразу,
   а не выяснять это в Фазе 2.
3. RSS у Дзен / YouTube / Habr есть не всегда — проверяем живым запросом.
"""

import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "_raw"


def vis_of(fname: str) -> str:
    p = RAW / f"{fname}.html"
    if not p.exists():
        return ""
    return probe.visible_text(p.read_text(encoding="utf-8"))


def same_page(a: str, b: str) -> float:
    """Насколько две страницы — одно и то же. Сравниваем видимый текст."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a[:20000], b[:20000]).quick_ratio()


def check_telegram(url: str) -> dict:
    """t.me/<name> -> можно ли читать через t.me/s/<name>."""
    name = urlparse(url).path.strip("/").split("/")[0]
    if not name or name.startswith("+") or name in ("joinchat", "share", "addstickers"):
        return {"channel": name, "readable": False, "why": "инвайт-ссылка, не публичный канал"}
    r = probe.fetch(f"https://t.me/s/{name}")
    if r["status"] != 200:
        return {"channel": name, "readable": False, "why": f"HTTP {r['status']}"}
    posts = len(re.findall(r'class="tgme_widget_message[ "]', r["text"]))
    if posts == 0:
        why = "нет постов в веб-версии: личный аккаунт, бот или закрытый канал"
        return {"channel": name, "readable": False, "why": why}
    dates = re.findall(r'datetime="(\d{4}-\d{2}-\d{2})', r["text"])
    return {
        "channel": name,
        "readable": True,
        "url": f"https://t.me/s/{name}",
        "posts_on_page": posts,
        "last_post": max(dates) if dates else None,
    }


def check_vk(url: str) -> dict:
    name = urlparse(url).path.strip("/").split("/")[0]
    if not name or name in ("share.php", "away.php"):
        return {"screen_name": name, "public": False, "why": "не страница сообщества"}
    r = probe.fetch(f"https://vk.com/{name}")
    ok = r["status"] == 200
    return {
        "screen_name": name,
        "public": ok,
        "url": f"https://vk.com/{name}",
        "note": "чтение постов — через API wall.get, нужен токен (Фаза 2)",
        "http": r["status"],
    }


def check_rss(url: str) -> dict:
    """Пробуем типовые RSS-адреса для найденной площадки."""
    host = urlparse(url).netloc.lower()
    path = urlparse(url).path.strip("/")
    cands = []
    if "youtube" in host:
        m = re.search(r"channel/(UC[\w-]+)", url)
        if m:
            cands.append(f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}")
    elif "dzen" in host or "zen.yandex" in host:
        cands.append(f"https://dzen.ru/{path}?rss")
        cands.append(f"https://dzen.ru/{path}/rss")
    elif "habr" in host:
        cands.append(f"https://habr.com/ru/rss/{path}/")
    elif "vc.ru" in host:
        cands.append(f"https://vc.ru/rss/{path}")
    for c in cands:
        r = probe.fetch(c)
        body = r["text"][:800].lstrip()
        is_feed = r["status"] == 200 and ("<rss" in body or "<feed" in body or "<?xml" in body)
        if is_feed:
            items = len(re.findall(r"<item[ >]|<entry[ >]", r["text"]))
            return {"rss": c, "ok": True, "items": items}
    return {"rss": cands[0] if cands else None, "ok": False}


def main():
    data = json.load((ROOT / "data" / "phase0-stage-b.json").open(encoding="utf-8"))
    out = []

    for rec in data:
        domain = rec["domain"]
        print(f"\n=== {domain}")
        home_vis = vis_of(f"home__{domain}")

        # 1. soft-404
        for kind in ("pricing", "integrations", "blog"):
            page = rec["pages"].get(kind)
            if not page:
                continue
            page_vis = vis_of(f"{kind}__{domain}")
            ratio = same_page(home_vis, page_vis)
            page["similarity_to_home"] = round(ratio, 3)
            same_url = (page.get("final_url") or "").rstrip("/") == rec["home"]["url"].rstrip("/")
            if ratio > 0.97 or same_url:
                page["valid"] = False
                page["why"] = "soft-404: сайт отдал главную вместо этой страницы"
                print(f"  {kind:13} ОТБРОШЕНА (копия главной, sim={ratio:.2f})")
            else:
                page["valid"] = True
                print(f"  {kind:13} ok  sim={ratio:.2f}  {page['url']}")

        # 2. соцканалы
        checked = {}
        for net, urls in rec.get("socials", {}).items():
            res = []
            for u in urls[:4]:
                if net == "telegram":
                    res.append(check_telegram(u) | {"src": u})
                elif net == "vk":
                    res.append(check_vk(u) | {"src": u})
                else:
                    res.append(check_rss(u) | {"src": u})
            checked[net] = res
            for r in res:
                mark = "OK " if (r.get("readable") or r.get("public") or r.get("ok")) else "нет"
                print(f"  {net:13} {mark} {r.get('src')}  {r.get('why', '')}")
        rec["socials_checked"] = checked
        out.append(rec)

    probe.dump_json("phase0-stage-c", out)
    print("\nsaved -> radar/data/phase0-stage-c.json")


if __name__ == "__main__":
    main()
