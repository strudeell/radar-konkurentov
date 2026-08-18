"""Фаза 0, шаг F: добываем RSS-ленты для YouTube и Дзена.

Ссылки с главных ведут на человекочитаемые адреса (youtube.com/@bewiseai),
а лента живёт по channel_id (UC...). Идентификатор достаём со страницы канала —
догадаться его нельзя.

Дзен отдаёт RSS не у всех каналов, поэтому проверяем несколько вариантов адреса
и честно фиксируем, если ленты нет.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe

ROOT = Path(__file__).resolve().parent.parent

CHANNEL_ID = re.compile(r'"(?:channelId|externalId)"\s*:\s*"(UC[\w-]{20,})"')
CANON = re.compile(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"')


def youtube_feed(url: str) -> dict:
    m = re.search(r"(UC[\w-]{20,})", url)
    if m:
        cid = m.group(1)
    else:
        page = probe.fetch(url, ua=probe.UA_BROWSER, timeout=20, retries=1)
        if page["status"] != 200:
            return {"src": url, "ok": False, "why": f"страница канала: HTTP {page['status']}"}
        hit = CHANNEL_ID.search(page["text"])
        if not hit:
            return {"src": url, "ok": False, "why": "channel_id на странице не найден"}
        cid = hit.group(1)

    feed = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
    r = probe.fetch(feed, timeout=20, retries=1)
    if r["status"] != 200 or "<feed" not in r["text"][:600]:
        return {"src": url, "ok": False, "channel_id": cid, "why": f"лента: HTTP {r['status']}"}
    titles = re.findall(r"<title>([^<]+)</title>", r["text"])[1:]
    dates = re.findall(r"<published>(\d{4}-\d{2}-\d{2})", r["text"])
    return {"src": url, "ok": True, "channel_id": cid, "rss": feed,
            "entries": len(dates), "last": max(dates) if dates else None,
            "sample": titles[:3]}


def dzen_feed(url: str) -> dict:
    path = url.split("dzen.ru/")[-1].split("?")[0].strip("/")
    for cand in (f"https://dzen.ru/{path}?rss", f"https://dzen.ru/{path}/rss",
                 f"https://dzen.ru/media/{path}?rss"):
        r = probe.fetch(cand, timeout=20, retries=1)
        body = r["text"][:600].lstrip()
        if r["status"] == 200 and ("<rss" in body or "<feed" in body):
            dates = re.findall(r"<pubDate>([^<]+)</pubDate>", r["text"])
            titles = re.findall(r"<title>(?:<!\[CDATA\[)?([^<\]]+)", r["text"])[1:]
            return {"src": url, "ok": True, "rss": cand, "entries": len(dates),
                    "last": dates[0] if dates else None, "sample": titles[:3]}
    return {"src": url, "ok": False, "why": "RSS по типовым адресам не отдаётся"}


def main():
    stage_c = json.load((ROOT / "data" / "phase0-stage-c.json").open(encoding="utf-8"))
    out = {}
    for rec in stage_c:
        dom = rec["domain"]
        socials = rec.get("socials", {})
        res = []
        for u in socials.get("youtube", []):
            if "/watch" in u:          # ссылка на отдельное видео, не канал
                continue
            res.append({"net": "youtube", **youtube_feed(u)})
        for u in socials.get("dzen", []):
            res.append({"net": "dzen", **dzen_feed(u)})
        if res:
            out[dom] = res
            for r in res:
                mark = "OK " if r["ok"] else "нет"
                print(f"{dom:22} {r['net']:8} {mark} {r.get('rss') or r.get('why')}"
                      f"  последняя: {r.get('last')}", flush=True)

    probe.dump_json("phase0-feeds", out)
    print("\nsaved -> radar/data/phase0-feeds.json")


if __name__ == "__main__":
    main()
