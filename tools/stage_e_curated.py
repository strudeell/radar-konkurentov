"""Фаза 0, шаг E: выверенный список источников + финальная проверка.

Автоматика (шаги A–B3) нашла кандидатов, sitemap дал точные адреса разделов.
Здесь список сведён вручную: у каждого конкурента ровно те страницы, которые
у него реально есть. Пустое место значит «страницы нет», а не «не искали».

Каждый URL ниже проверяется живым запросом прямо сейчас. Что не ответило —
в config/sources.yaml не попадёт.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe
from stage_b3_repick import CAPTCHA_RE

ROOT = Path(__file__).resolve().parent.parent

# kind: home | pricing | integrations | blog | cases | extra
# Комментарий — откуда взят адрес и почему именно он.
CURATED = {
    "rechka.ai": {
        "home": "https://rechka.ai/",
        "blog": "https://rechka.ai/blog",      # ссылка с главной, в sitemap её нет
        "cases": "https://rechka.ai/cases",
        # тарифов на сайте нет — только форма расчёта, подтверждено sitemap (8 URL)
    },
    "salesai.ru": {
        "home": "https://salesai.ru/",
        "pricing": "https://salesai.ru/pricing/",
        "blog": "https://salesai.ru/articles/",
        "cases": "https://salesai.ru/cases/",
        "extra": "https://salesai.ru/llms.txt",  # машиночитаемая выжимка оффера
        # общей страницы интеграций нет: только по одной на CRM (/solutions/...)
    },
    "imot.io": {
        "home": "https://imot.io/",
        "pricing": "https://imot.io/tarif",     # из sitemap; в аналитике значилось «цен нет»
        "blog": "https://imot.io/media",
        "cases": "https://imot.io/cases",
    },
    "recall-ai.ru": {
        "home": "https://recall-ai.ru/",
        "pricing": "https://recall-ai.ru/tariffs",
        "blog": "https://recall-ai.ru/articles",
    },
    "mango-office.ru": {
        # не корень: сайт телефонии огромный, нам нужен именно продукт речевой аналитики
        "home": "https://www.mango-office.ru/products/virtualnaya_ats/vozmozhnosti/speech-analytics/",
        "pricing": "https://www.mango-office.ru/products/virtualnaya_ats/price/",
        "integrations": "https://www.mango-office.ru/products/integraciya/marketplace/",
        "blog": "https://www.mango-office.ru/journal/",
    },
    "roistat.com": {
        "home": "https://roistat.com/ru/",
        "pricing": "https://roistat.com/ru/price",
        "integrations": "https://roistat.com/ru/features/integrations",
        "blog": "https://roistat.com/rublog/",
    },
    "aistudio.yandex.ru": {
        "home": "https://aistudio.yandex.ru/ru/solutions/speechsense",
        # внутренние страницы отдают SmartCaptcha — автоматически не берутся
    },
    "rodnik.bz": {
        "home": "https://rodnik.bz/",           # одностраничник, цены на главной
        "extra": "https://rodnik.bz/manifesto.html",
    },
    "bewise.ai": {
        "home": "https://bewise.ai/",
        "pricing": "https://bewise.ai/pricing/",
        "blog": "https://bewise.ai/blog/",
        "cases": "https://bewise.ai/cases/",
    },
    "okk.ectem.ru": {
        "home": "https://okk.ectem.ru/",        # одностраничник: тарифы на главной
        "pricing": "https://okk.ectem.ru/sbercrmtariffs",  # отдельная линейка под Сбер
    },
    "call-intellect.ru": {},                    # недоступен, см. отчёт
    "phonix.pro": {
        "home": "https://phonix.pro/",
        "blog": "https://phonix.pro/blog",
        # /pricing и /integrations отдают 200, но это заглушка SPA. В sitemap их нет
    },
    "zvonalitik.ru": {
        "home": "https://zvonalitik.ru/",       # тарифы на главной
        "blog": "https://zvonalitik.ru/articles/",
    },
    "qolio.ru": {
        "home": "https://qolio.ru/",
        "pricing": "https://qolio.ru/annual_price",
        "integrations": "https://qolio.ru/integrations",
        "blog": "https://qolio.ru/blog",
    },
    "speechanalytics.ru": {
        "home": "https://speechanalytics.ru/",
        "extra": "https://speechanalytics.ru/partners",
    },
    "dialext.com": {
        "home": "https://dialext.com/",
        "blog": "https://dialext.com/blog/",
        "integrations": "https://dialext.com/amocrm/",  # индекса нет, лендинги по CRM
    },
    "getcalls.ru": {
        "home": "https://getcalls.ru/",         # одностраничник без sitemap
    },
    "oneboost.io": {
        "home": "https://oneboost.io/ru/",
        "cases": "https://oneboost.io/case_foxford",
    },
    "iqstat.io": {
        "home": "https://iqstat.io/ru",         # корень / почти пустой, рабочий раздел — /ru
        "blog": "https://iqstat.io/ru/blog/",
    },
}


def main():
    out = {}
    for domain, pages in CURATED.items():
        out[domain] = {}
        if not pages:
            print(f"{domain:22} — пропуск (недоступен)")
            continue
        for kind, url in pages.items():
            r = probe.fetch(url, timeout=20.0, retries=1)
            vis = probe.visible_text(r["text"]) if r["text"] else ""
            if r["status"] != 200:
                verdict = f"http {r['status']}" if r["status"] else f"ошибка: {r['error'][:40]}"
            elif CAPTCHA_RE.search(vis[:1500]):
                verdict = "капча"
            elif len(vis) < 200:
                verdict = f"пусто ({len(vis)})"
            else:
                verdict = "ok"
                probe.save_raw(f"final__{kind}__{domain}", r["text"])
            out[domain][kind] = {
                "url": url, "final_url": r["final_url"], "status": r["status"],
                "visible_chars": len(vis), "verdict": verdict,
            }
            print(f"{domain:22} {kind:13} {verdict:16} {len(vis):>7} симв.  {url}",
                  flush=True)

    probe.dump_json("phase0-final-pages", out)
    print("\nsaved -> radar/data/phase0-final-pages.json")


if __name__ == "__main__":
    main()
