"""Фаза 0, шаг B2: sitemap.xml для сайтов, где навигация на JS.

На Tilda / React ссылок в HTML может не быть вовсе, и типовые пути не угадываются.
Sitemap отдаёт реальный список страниц — это надёжнее догадок.
Запускается точечно, только по доменам, где шаг B ничего не нашёл.
"""

import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe

PRICE_RE = re.compile(r"price|pricing|tarif|tariff|cena|ceny|cost|stoimost|plan", re.I)
INTEG_RE = re.compile(r"integr|connect", re.I)
BLOG_RE = re.compile(r"blog|news|novosti|article|stati|media|case|keys", re.I)


def sitemap_urls(base: str, depth: int = 0, seen: set | None = None) -> list[str]:
    """Разворачивает sitemap, включая индексные (sitemap of sitemaps)."""
    seen = seen if seen is not None else set()
    if base in seen or depth > 2:
        return []
    seen.add(base)
    r = probe.fetch(base)
    if r["status"] != 200 or "<" not in r["text"][:200]:
        return []
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r["text"])
    urls, nested = [], []
    for u in locs:
        (nested if u.endswith((".xml", ".xml.gz")) else urls).append(u)
    for n in nested[:5]:
        urls += sitemap_urls(n, depth + 1, seen)
    return urls


def main(domains: list[str]):
    for dom in domains:
        print(f"\n=== {dom}")
        found = []
        for path in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml"):
            found = sitemap_urls(f"https://{dom}{path}")
            if found:
                print(f"  sitemap: {path} -> {len(found)} URL")
                break
        if not found:
            print("  sitemap: НЕТ")
            continue
        for label, rx in (("ТАРИФЫ", PRICE_RE), ("ИНТЕГРАЦИИ", INTEG_RE), ("БЛОГ", BLOG_RE)):
            hits = [u for u in found if rx.search(urlparse(u).path)]
            print(f"  {label}: {hits[:6] if hits else 'нет'}")
        others = [u for u in found if not any(r.search(urlparse(u).path)
                                              for r in (PRICE_RE, INTEG_RE, BLOG_RE))]
        print(f"  прочие ({len(others)}): {others[:12]}")


if __name__ == "__main__":
    main(sys.argv[1:])
