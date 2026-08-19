#!/usr/bin/env python3
"""Сборщик снимков — Фаза 2.

Раз в день заходит на все страницы и каналы из sources.yaml, вынимает из каждой
видимый текст, очищает его от самоменяющегося и кладёт в файл:

    snapshots/<домен>/<страница>/<ГГГГ-ММ-ДД>.txt

Завтрашний файл сравнит с сегодняшним следующая фаза. Здесь сравнение только
одно и самое грубое: изменился текст или нет.

Что важно знать про поведение сборщика.

**Первый снимок источника — не изменение, а точка отсчёта.** Иначе в первый день
прилетело бы шестьдесят «изменений» на пустом месте.

**Одинаковый текст второй раз не сохраняется.** Если за сутки на странице ничего
не поменялось, новый файл не создаётся: он был бы точной копией вчерашнего.
За год таких копий накопилось бы 365 штук на каждую страницу. В отчёте прогона
при этом честно записано, что источник проверен и не изменился.

**Подозрительный снимок не сохраняется вообще.** Если со страницы вдруг пришло
двести символов вместо двенадцати тысяч — это почти всегда поломка сборщика или
защита сайта, а не то, что конкурент стёр свой сайт. Записать такое в снимки
нельзя: завтра радар отчитается о гигантском изменении, послезавтра — о таком же
откате назад, и оба сообщения будут ложью. Вчерашний снимок остаётся нетронутым,
а строка «требует внимания» уходит в отчёт прогона.

**Падение одного источника не роняет прогон.** Каждый источник живёт сам по себе,
итог по каждому — в runs/<ГГГГ-ММ-ДД>.json.

Запуск:

    python collect.py                    полный обход
    python collect.py --only rechka.ai   только один домен (ключ можно повторять)
    python collect.py --pages            только сайты, без соцсетей
    python collect.py --channels         только соцсети
    python collect.py --no-vk            пропустить ВКонтакте
    python collect.py --dry-run          обойти и показать, но ничего не сохранять
    python collect.py --prune-only       только вычистить снимки старше 90 дней
"""

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools"))

import yaml  # noqa: E402

import channels  # noqa: E402
import normalize  # noqa: E402
import probe  # noqa: E402
from robots import Robots  # noqa: E402

SNAPSHOTS = ROOT / "snapshots"
RUNS = ROOT / "runs"

# Значения по умолчанию. Человек меняет их в config.yaml, раздел collect.
DEFAULTS = {
    "delay_sec": 2.0,          # пауза между обращениями к одному сайту
    "timeout_sec": 20.0,       # сколько ждём ответа
    "retries": 2,              # столько раз повторяем при сбое
    "respect_robots": True,    # соблюдать ли правила сайта для роботов
    "min_visible_chars": 200,  # меньше этого — сломался сборщик, а не сайт опустел
    "shrink_ratio": 0.4,       # усох сильнее, чем до этой доли эталона, — тоже подозрение
    "keep_days": 90,           # сколько дней держим полные снимки
    "telegram_posts": 30,      # сколько последних публикаций берём из канала
    "vk_posts": 20,
}

# Статусы источника в отчёте прогона. Их читает человек, поэтому по-русски.
S_BASELINE = "точка отсчёта"
S_CHANGED = "изменилось"
S_SAME = "без изменений"
S_SUSPECT = "подозрение"
S_ERROR = "ошибка"
S_BLOCKED = "закрыто сайтом"
S_SKIPPED = "пропущено"


def load_yaml(name: str) -> dict:
    path = ROOT / name
    if not path.exists():
        sys.exit(f"Не найден {name} рядом с collect.py.")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot_dir(domain: str, page: str) -> Path:
    return SNAPSHOTS / domain / page


def previous_snapshot(folder: Path, today: str) -> Path | None:
    """Последний снимок, снятый раньше сегодняшнего дня."""
    if not folder.exists():
        return None
    earlier = sorted(p for p in folder.glob("*.txt") if p.stem < today)
    return earlier[-1] if earlier else None


def save_snapshot(folder: Path, today: str, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{today}.txt"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def save_broken_html(domain: str, page: str, today: str, html: str) -> None:
    """Разметку подозрительной страницы кладём рядом — чтобы было что смотреть.

    Папка data/_raw закрыта от git и чистится руками: это материал для
    разбирательства, а не часть проекта.
    """
    if not html:
        return
    probe.RAW.mkdir(parents=True, exist_ok=True)
    name = f"подозрение__{domain}__{page}__{today}.html"
    (probe.RAW / name).write_text(html, encoding="utf-8")


def build_targets(sources: dict, only: list[str], want_pages: bool,
                  want_channels: bool, want_vk: bool) -> tuple[list[dict], list[dict]]:
    """Список того, что обходим, и список того, что сознательно пропущено."""
    targets: list[dict] = []
    skipped: list[dict] = []
    for comp in sources.get("competitors", []):
        domain = comp["domain"]
        if only and domain.lower() not in only and comp["name"].lower() not in only:
            continue

        if comp.get("status") != "ok":
            reason = {
                "dead": "сайт не отвечает никому, игрок считается мёртвым",
                "blocked": "защита от роботов, смотрим глазами раз в месяц",
            }.get(comp.get("status"), str(comp.get("status")))
            skipped.append({
                "competitor": comp["name"], "domain": domain, "page": "—",
                "url": None, "how": "—", "priority": "normal",
                "http_status": None, "chars": None, "prev_chars": None,
                "delta_chars": None, "sha256": None, "snapshot": None,
                "status": S_SKIPPED, "note": reason,
            })
            continue

        if want_pages:
            for page in comp.get("pages", []):
                targets.append({
                    "competitor": comp["name"], "domain": domain,
                    "page": page["kind"], "url": page["url"], "how": "http",
                    "priority": page.get("priority", "normal"),
                    "baseline_chars": page.get("baseline_visible_chars"),
                    "is_home": page["kind"] == "home",
                })

        if want_channels:
            for ch in comp.get("channels", []):
                net = ch["net"]
                if net == "vk" and not want_vk:
                    skipped.append({
                        "competitor": comp["name"], "domain": domain,
                        "page": f"vk-{ch['id']}", "url": ch.get("url"), "how": "vk",
                        "priority": "normal", "http_status": None, "chars": None,
                        "prev_chars": None, "delta_chars": None, "sha256": None,
                        "snapshot": None, "status": S_SKIPPED,
                        "note": "ВКонтакте пропущен по ключу --no-vk",
                    })
                    continue
                targets.append({
                    "competitor": comp["name"], "domain": domain,
                    "page": f"{net}-{ch['id']}", "url": ch["url"], "how": net,
                    "priority": "normal", "baseline_chars": None,
                    "channel_id": ch["id"], "is_home": False,
                })
    return targets, skipped


def collect_http(target: dict, cfg: dict, robots: Robots | None) -> dict:
    """Обычная страница сайта или канал Telegram."""
    url = target["url"]
    out: dict = {"http_status": None, "error": None, "text": None, "html": ""}

    if robots is not None:
        allowed, why = robots.allowed(url)
        if not allowed:
            out["error"] = why
            out["blocked"] = True
            return out
        delay = robots.crawl_delay(url)
        if delay:
            probe.HOST_DELAY[urlparse(url).netloc] = delay

    res = probe.fetch(url, timeout=float(cfg["timeout_sec"]), retries=int(cfg["retries"]))
    out["http_status"] = res["status"]
    out["html"] = res["text"]
    if res["error"]:
        out["error"] = res["error"]
        return out
    if res["status"] != 200:
        out["error"] = f"сайт ответил кодом {res['status']}"
        return out

    host = urlparse(res["final_url"] or url).netloc
    if target["how"] == "telegram":
        text, posts = channels.telegram_snapshot(res["text"], int(cfg["telegram_posts"]))
        out["text"] = text
        out["posts"] = posts
        if posts == 0:
            out["error"] = "в канале не нашлось ни одной публикации"
            out["text"] = None
        return out

    is_html = "html" in (res["content_type"] or "").lower() or "<" in res["text"][:200]
    # mask=False: в снимок ложится то, что написано на странице. Метки шумодава
    # («<дата>» вместо «1 сентября 2026 года») нужны сравнению, а не читателю, и
    # ставит их detect в момент сравнения. Пока метки ставились здесь, срочное
    # сообщение о смене тарифов уходило владельцу без даты, с которой цены
    # меняются, — она была стёрта ещё при сохранении снимка.
    out["text"] = normalize.to_snapshot(res["text"], host, is_html=is_html,
                                        kind=target["page"], mask=False)
    return out


def collect_vk(target: dict, cfg: dict, token: str | None) -> dict:
    out: dict = {"http_status": None, "error": None, "text": None, "html": ""}
    if not token:
        out["error"] = ("нет ключа доступа к ВКонтакте: "
                        "python vk_token.py auth <номер приложения>")
        return out
    text, posts, err = channels.vk_snapshot(target["channel_id"], token,
                                            int(cfg["vk_posts"]))
    if err:
        out["error"] = f"ВКонтакте отказал: {err}"
        return out
    if posts == 0:
        out["error"] = "в сообществе не нашлось ни одной публикации"
        return out
    out["text"] = text
    out["posts"] = posts
    return out


def judge(target: dict, text: str, cfg: dict, home_hashes: dict) -> str | None:
    """Санитарная проверка снимка. Возвращает причину подозрения или None.

    Обе проверки — из отчёта Фазы 0. Первая: текста слишком мало, значит сломался
    сборщик, а не сайт. Вторая: текст совпал с главной страницей того же сайта —
    значит, такой страницы у конкурента нет, сайт отдаёт главную на любой адрес.

    К каналам соцсетей проверка на объём не применяется, и это выяснилось на
    живых данных: сообщество Speech Analytics во ВКонтакте держит ровно одну
    публикацию от 2018 года на 79 символов. Для страницы сайта такой объём —
    поломка, для канала — нормальная жизнь. Пустоту канала ловим не объёмом,
    а числом публикаций: ноль постов — это ошибка, она разбирается при сборе.
    """
    if target["how"] != "http":
        return None

    chars = len(text)
    if chars < int(cfg["min_visible_chars"]):
        return (f"пришло всего {chars} символов — это похоже на поломку сбора, "
                "а не на опустевшую страницу")

    baseline = target.get("baseline_chars")
    if baseline and chars < baseline * float(cfg["shrink_ratio"]):
        return (f"текста стало {chars} символов вместо {baseline} в эталоне — "
                "усох слишком сильно, надо смотреть глазами")

    if not target["is_home"] and target["how"] == "http":
        home = home_hashes.get(target["domain"])
        if home and home == sha256(text):
            return "текст совпал с главной — сайт отдаёт главную вместо этой страницы"
    return None


def prune(keep_days: int, dry_run: bool) -> list[str]:
    """Чистка полных снимков старше срока. Самый свежий не трогаем никогда.

    Диффы (следующая фаза) храним всегда: они маленькие, и в них вся ценность.
    Полные снимки — самое тяжёлое в проекте и самое бесполезное спустя квартал.
    """
    edge = (date.today() - timedelta(days=keep_days)).isoformat()
    removed = []
    for folder in SNAPSHOTS.glob("*/*"):
        if not folder.is_dir():
            continue
        files = sorted(folder.glob("*.txt"))
        for path in files[:-1]:          # последний снимок остаётся всегда
            if path.stem < edge:
                removed.append(str(path.relative_to(ROOT)).replace("\\", "/"))
                if not dry_run:
                    path.unlink()
    return removed


def merge_into_day(report_path: Path, fresh: list[dict]) -> list[dict]:
    """Свести результаты этого захода с тем, что уже собрано за сегодня.

    Зачем. Отчёт runs/<дата>.json — это состояние дня, а не одного запуска.
    Прогон по одному домену (--only) не должен стирать из него остальные
    шестьдесят источников: иначе расписание в Фазе 5 решит, что сбор прошёл
    наполовину, а недельный дайджест недосчитается конкурентов.

    Правило слияния: свежий результат заменяет прежний по паре «домен +
    страница». Исключение — «пропущено»: если источник уже собран сегодня
    удачно, а в этот заход его отсекли ключом запуска, в отчёте остаётся удачный
    результат. Пропуск по ключу — это про запуск, а не про источник.
    """
    day: dict[tuple[str, str], dict] = {}
    if report_path.exists():
        try:
            was = json.loads(report_path.read_text(encoding="utf-8"))
            for item in was.get("items", []):
                day[(item["domain"], item["page"])] = item
        except (json.JSONDecodeError, OSError):
            pass

    for item in fresh:
        key = (item["domain"], item["page"])
        already = day.get(key)
        if already and item["status"] == S_SKIPPED and already["status"] != S_SKIPPED:
            continue
        day[key] = item

    return sorted(day.values(), key=lambda i: (i["domain"], i["page"]))


def main() -> int:
    ap = argparse.ArgumentParser(description="Сборщик снимков радара")
    ap.add_argument("--only", action="append", default=[],
                    help="собрать только этот домен (ключ можно повторять)")
    ap.add_argument("--pages", action="store_true", help="только сайты")
    ap.add_argument("--channels", action="store_true", help="только соцсети")
    ap.add_argument("--no-vk", action="store_true", help="пропустить ВКонтакте")
    ap.add_argument("--dry-run", action="store_true", help="ничего не сохранять")
    ap.add_argument("--prune-only", action="store_true",
                    help="только вычистить старые снимки")
    ap.add_argument("--no-robots", action="store_true",
                    help="не читать robots.txt (только для отладки)")
    args = ap.parse_args()

    config = load_yaml("config.yaml")
    cfg = {**DEFAULTS, **(config.get("collect") or {})}
    today = date.today().isoformat()

    if args.prune_only:
        removed = prune(int(cfg["keep_days"]), args.dry_run)
        print(f"Снимков старше {cfg['keep_days']} дней: {len(removed)}")
        for line in removed:
            print("  удалён", line)
        return 0

    sources = load_yaml("sources.yaml")
    want_pages = args.pages or not args.channels
    want_channels = args.channels or not args.pages
    only = [o.lower() for o in args.only]
    targets, results = build_targets(sources, only, want_pages, want_channels,
                                     not args.no_vk)

    if not targets:
        print("Нечего собирать: проверьте ключ --only.")
        return 1

    probe.DELAY_SAME_HOST = float(cfg["delay_sec"])
    robots = None
    if cfg["respect_robots"] and not args.no_robots:
        robots = Robots(
            lambda u: probe.fetch(u, timeout=float(cfg["timeout_sec"]), retries=1),
            probe.UA_BOT,
        )

    # Ключ ВК живёт час, поэтому обновляем его перед сбором, а не надеемся на удачу.
    token = None
    if any(t["how"] == "vk" for t in targets):
        channels.vk_refresh()
        token = channels.vk_token()

    # Главные страницы идут первыми: с ними сверяются остальные страницы того же
    # сайта, чтобы поймать сайты, отдающие главную на любой адрес.
    targets.sort(key=lambda t: (t["domain"], not t["is_home"]))

    started = datetime.now(timezone.utc)
    home_hashes: dict[str, str] = {}
    print(f"Обход {len(targets)} источников. Сегодня {today}.\n")

    for target in targets:
        label = f"{target['competitor']} · {target['page']}"
        if target["how"] == "vk":
            got = collect_vk(target, cfg, token)
        else:
            got = collect_http(target, cfg, robots)

        item = {
            "competitor": target["competitor"], "domain": target["domain"],
            "page": target["page"], "url": target["url"], "how": target["how"],
            "priority": target["priority"], "http_status": got["http_status"],
            "chars": None, "prev_chars": None, "delta_chars": None,
            "sha256": None, "snapshot": None, "status": None, "note": None,
        }

        if got.get("blocked"):
            item["status"], item["note"] = S_BLOCKED, got["error"]
            print(f"  закрыто    {label}: {got['error']}")
            results.append(item)
            continue

        if got["error"] or got["text"] is None:
            item["status"], item["note"] = S_ERROR, got["error"]
            print(f"  ОШИБКА     {label}: {got['error']}")
            results.append(item)
            continue

        text = got["text"]
        item["chars"] = len(text)
        item["sha256"] = sha256(text)
        if target["is_home"]:
            home_hashes[target["domain"]] = item["sha256"]

        doubt = judge(target, text, cfg, home_hashes)
        if doubt:
            item["status"], item["note"] = S_SUSPECT, doubt
            print(f"  ВНИМАНИЕ   {label}: {doubt}")
            if not args.dry_run:
                save_broken_html(target["domain"], target["page"], today, got["html"])
            results.append(item)
            continue

        folder = snapshot_dir(target["domain"], target["page"])
        prev = previous_snapshot(folder, today)
        if prev is None:
            item["status"] = S_BASELINE
            item["note"] = "первый снимок источника, сравнивать пока не с чем"
        else:
            old = prev.read_text(encoding="utf-8")
            item["prev_chars"] = len(old)
            item["delta_chars"] = len(text) - len(old)
            item["status"] = S_SAME if sha256(old) == item["sha256"] else S_CHANGED
            item["note"] = f"сравнение со снимком от {prev.stem}"

        if item["status"] == S_SAME:
            item["snapshot"] = str(prev.relative_to(ROOT)).replace("\\", "/")
        elif not args.dry_run:
            path = save_snapshot(folder, today, text)
            item["snapshot"] = str(path.relative_to(ROOT)).replace("\\", "/")

        mark = {S_BASELINE: "отсчёт", S_CHANGED: "ИЗМЕНЕНО", S_SAME: "как вчера"}[item["status"]]
        delta = "" if not item["delta_chars"] else f"  ({item['delta_chars']:+d} символов)"
        print(f"  {mark:<10} {label}: {item['chars']} символов{delta}")
        results.append(item)

    finished = datetime.now(timezone.utc)
    removed = [] if args.dry_run else prune(int(cfg["keep_days"]), False)
    duration = round((finished - started).total_seconds(), 1)

    counts: dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1

    filters = " ".join(
        ["--only " + o for o in args.only]
        + (["--pages"] if args.pages else [])
        + (["--channels"] if args.channels else [])
        + (["--no-vk"] if args.no_vk else [])
    )
    report_path = RUNS / f"{today}.json"
    passes = []
    if report_path.exists():
        try:
            passes = json.loads(report_path.read_text(encoding="utf-8")).get("passes", [])
        except (json.JSONDecodeError, OSError):
            passes = []
    passes.append({
        "фильтр": filters or "полный обход",
        "started": started.isoformat(timespec="seconds"),
        "finished": finished.isoformat(timespec="seconds"),
        "duration_sec": duration,
        "источников в заходе": len(results),
    })

    day_items = merge_into_day(report_path, results)
    day_counts: dict[str, int] = {}
    for item in day_items:
        day_counts[item["status"]] = day_counts.get(item["status"], 0) + 1
    attention = [f"{i['competitor']} · {i['page']}: {i['note']}"
                 for i in day_items if i["status"] in (S_ERROR, S_SUSPECT, S_BLOCKED)]

    report = {
        "date": today,
        "started": passes[0]["started"],
        "finished": finished.isoformat(timespec="seconds"),
        "duration_sec": duration,
        "user_agent": probe.UA_BOT,
        "dry_run": args.dry_run,
        "totals": {"источников": len(day_items), **day_counts},
        "attention": attention,
        "pruned_snapshots": removed,
        "passes": passes,
        "items": day_items,
    }

    if not args.dry_run:
        RUNS.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nГотово за {duration} с.")
    for status, number in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {status}: {number}")
    if removed:
        print(f"  вычищено снимков старше {cfg['keep_days']} дней: {len(removed)}")
    if attention:
        print("\nТребует внимания:")
        for line in attention:
            print("  •", line)
    if args.dry_run:
        print("\nЭто был холостой прогон: ничего не сохранено.")
    else:
        print(f"\nОтчёт прогона: runs/{today}.json")

    # Ненулевой код возврата — сигнал для расписания в Фазе 5.
    return 1 if counts.get(S_ERROR) else 0


if __name__ == "__main__":
    sys.exit(main())
