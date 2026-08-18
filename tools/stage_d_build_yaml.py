"""Фаза 0, шаг D: сборка sources.yaml — результат фазы.

Источники данных:
  phase0-final-pages.json — выверенный список страниц, каждая проверена запросом;
  phase0-stage-c.json     — соцканалы, проверенные открытием.

У каждого источника явно указан способ обхода (`fetch`), потому что не всё
берётся простым запросом:
  http    — обычный GET + нормализация в видимый текст (основной путь);
  api     — официальный API площадки (ВК: wall.get, нужен токен);
  rss     — лента;
  browser — простым запросом не берётся, нужна браузерная автоматизация;
  manual  — автоматически не берётся вообще, проверять руками;
  none    — не берётся никак: источник мёртв.
"""

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe

ROOT = Path(__file__).resolve().parent.parent

# priority: critical — изменение идёт мгновенным алертом (класс «критично» из 03),
#           normal   — копится в недельный дайджест.
KIND_PRIORITY = {
    "home": "critical",       # смена главного обещания — сигнал №3
    "pricing": "critical",    # числа с тарифов — сигнал №1
    "integrations": "normal",
    "blog": "normal",
    "cases": "normal",
    "extra": "normal",
}

# Ручные пометки по итогам разведки: что нельзя взять простым запросом.
OVERRIDES = {
    "aistudio.yandex.ru": {
        "status": "blocked",
        "fetch": "manual",
        "note": "SmartCaptcha на всех страницах, включая страницу решения. "
                "Продукт в Private Preview, публично меняется редко — "
                "дешевле проверять руками раз в месяц, чем городить обход.",
    },
    "call-intellect.ru": {
        "status": "dead",
        "fetch": "none",
        "note": "Сайт не отвечает. TLS-рукопожатие обрывается сервером: DNS резолвится "
                "(45.67.59.118), TCP на 443 и 80 открывается, ответа нет. Одинаково для "
                "httpx, curl и браузера. 18.08.2026 перепроверено с мобильного интернета "
                "(российский IP) — результат тот же. Игрок считается мёртвым, из сбора "
                "исключён. Запись оставлена, чтобы не искать его заново.",
    },
    "iqstat.io": {
        "note": "Главная отдаёт 328 символов видимого текста — контент подгружается "
                "JS. Блог берётся нормально. Порог шумодава для главной не ставить, "
                "пока не проверено браузером.",
    },
    "phonix.pro": {
        "note": "SPA: любой несуществующий путь отдаёт 200 и заглушку. Проверять "
                "не код ответа, а объём видимого текста.",
    },
    "getcalls.ru": {
        "note": "Одностраничник на Tilda без sitemap и robots.txt. Тарифы — на главной.",
    },
    "okk.ectem.ru": {
        "note": "Одностраничник: тарифы на главной, отдельной страницы нет. "
                "robots.txt запрещает /tariffs_sber — не трогаем.",
    },
    "rodnik.bz": {
        "note": "Одностраничник, robots.txt отсутствует. Цены не публикуются.",
    },
    "roistat.com": {
        "note": "robots.txt закрывает /ru/blog/ и /rublog/* только для Yandex — "
                "это борьба с дублями в индексе, не запрет обхода. Для нас открыто.",
    },
    "salesai.ru": {
        "note": "Отдают /llms.txt — машиночитаемую выжимку оффера на 50 тыс. символов. "
                "Для отслеживания цен это лучше HTML: структурировано и меняется "
                "только по делу.",
    },
    "mango-office.ru": {
        "note": "Отслеживаем не корень сайта телефонии, а страницу продукта речевой "
                "аналитики и её тарифы — иначе прилетит вся маркетинговая активность "
                "оператора.",
    },
}

GAPS_NOTE = {
    "pricing": "цен на сайте нет",
    "integrations": "отдельной страницы интеграций нет",
    "blog": "блога/новостей нет",
}


def main():
    final = json.load((ROOT / "data" / "phase0-final-pages.json").open(encoding="utf-8"))
    stage_c = {r["domain"]: r for r in
               json.load((ROOT / "data" / "phase0-stage-c.json").open(encoding="utf-8"))}

    competitors = []
    for domain, pages in final.items():
        rec = stage_c.get(domain, {})
        ov = OVERRIDES.get(domain, {})

        src = []
        for kind, p in pages.items():
            if p["verdict"] != "ok":
                continue
            entry = {
                "kind": kind,
                "url": p["final_url"] or p["url"],
                "fetch": "http",
                "priority": KIND_PRIORITY.get(kind, "normal"),
                "baseline_visible_chars": p["visible_chars"],
            }
            if kind == "pricing" or (kind == "home" and "pricing" not in pages):
                # числа тянем отдельным разбором — сигнал №1 из 03
                entry["extract_numbers"] = True
            src.append(entry)

        channels = []
        for net, items in (rec.get("socials_checked") or {}).items():
            for it in items:
                if net == "telegram" and it.get("readable"):
                    channels.append({"net": "telegram", "id": it["channel"],
                                     "url": it["url"], "fetch": "http",
                                     "last_post_seen": it.get("last_post")})
                elif net == "vk" and it.get("public"):
                    channels.append({"net": "vk", "id": it["screen_name"],
                                     "url": it["url"], "fetch": "api",
                                     "note": "wall.get, нужен токен"})
                elif it.get("ok"):
                    channels.append({"net": net, "url": it["rss"], "fetch": "rss"})

        gaps = [GAPS_NOTE[k] for k in ("pricing", "integrations", "blog")
                if k not in pages or pages[k]["verdict"] != "ok"]

        entry = {
            "name": rec.get("name", domain),
            "domain": domain,
            "group": int(rec.get("group", 2)),
            "status": ov.get("status", "ok" if src else "unreachable"),
            "pages": src,
            "channels": channels,
            "gaps": gaps,
        }
        if ov.get("note"):
            entry["note"] = ov["note"]
        if ov.get("fetch") in ("manual", "none"):
            entry["check"] = ov["fetch"]
        competitors.append(entry)

    competitors.sort(key=lambda c: (c["group"], c["domain"]))

    doc = {
        "meta": {
            "generated": "2026-08-18",
            "phase": "0 — разведка источников",
            "rule": "Все URL проверены живым запросом. Пустое место означает, что "
                    "страницы у конкурента нет, а не что её не искали.",
            "user_agent": probe.UA_BOT,
            "user_agent_note": "контакт — публичный ящик с okk-ai.ru, поэтому админ "
                               "чужого сайта может сверить его с сайтом. Собирается "
                               "из config.yaml, менять там",
            "politeness": {"delay_sec": 2, "timeout_sec": 20, "retries": 2,
                           "respect_robots": True},
            "channels_policy": {
                "telegram": "берём: публичная веб-версия t.me/s/<канал> работает",
                "vk": "берём: официальный API wall.get, нужен токен приложения",
                "youtube": "НЕ берём: youtube.com не резолвится по DNS с рабочей "
                           "машины (getaddrinfo failed). Плюс смысла мало — видео "
                           "мы всё равно не смотрим, а факт публикации дублируется "
                           "в блоге и Telegram",
                "dzen": "НЕ берём автоматически: RSS не отдаётся, страницы канала "
                        "рисует JS (на любой адрес ~2,9 КБ каркаса). 18.08.2026 "
                        "проверены сплошь все 19 поиском внутри Дзена: каналы есть "
                        "у четырёх (Roistat, Call-Intellect, Ectem, Mango Office), "
                        "но все мёртвые — самый живой молчит 7 месяцев. Ради этого "
                        "поднимать браузерную автоматизацию невыгодно, смотреть руками",
                "vc_habr": "каналов не найдено ни у кого из 19",
            },
            "totals": {
                "competitors": len(competitors),
                "pages": sum(len(c["pages"]) for c in competitors),
                "channels": sum(len(c["channels"]) for c in competitors),
                "no_auto_collect": sum(1 for c in competitors if c["status"] != "ok"),
            },
        },
        "competitors": competitors,
    }

    out = ROOT / "sources.yaml"
    out.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100),
                   encoding="utf-8")
    t = doc["meta"]["totals"]
    print(f"конкурентов={t['competitors']} страниц={t['pages']} "
          f"каналов={t['channels']} без автосбора={t['no_auto_collect']}")
    print(f"saved -> {out}")


if __name__ == "__main__":
    main()
