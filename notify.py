#!/usr/bin/env python3
"""Классификация и уведомления — Фаза 4.

Детектор Фазы 3 отвечает на вопрос «что изменилось». Эта программа отвечает на
следующий: «беспокоить человека сейчас или подождать до понедельника».

Договорённость с заказчиком, ради которой всё и написано:

    обычные изменения    → копятся и уходят одной сводкой по понедельникам
    критичные            → проверяются каждый день и, если нашлись, уходят сразу

Отсюда два способа запуска, и они делают разное:

    python notify.py            ежедневно: найти критичное за сегодня и отправить
    python notify.py --digest   по понедельникам: собрать сводку за неделю

Что здесь важно знать.

**Одно и то же изменение не присылается дважды.** Программа помнит, о чём уже
писала, в notify/journal.json. Запуск повторно — обычное дело: расписание может
сработать дважды, человек может запустить руками. Второй раз человек ничего не
получит, и это правильно: радар, который дублирует алерты, читать перестают так
же быстро, как радар, который шумит.

**Молчание — это ответ.** Критичного нет — сообщения нет. Не «сегодня всё
спокойно» каждое утро: ежедневное «ничего не произошло» превращается в фон, на
фоне которого не видно настоящего алерта. Строка «ничего не менялось у …» есть,
но она в недельной сводке, где её читают осознанно.

**Без настроенного бота программа работает.** Она покажет сообщение на экране и
честно скажет, что не отправила. Это режим Фазы 6: неделя обкатки идёт без
боевой рассылки, сообщения смотрят глазами и калибруют правила.

Остальные ключи:

    python notify.py --date 2026-08-19   разобрать конкретный день
    python notify.py --dry-run           показать сообщение, ничего не слать
    python notify.py --resend            прислать заново, забыв про журнал
    python notify.py --to 123456789      отправить в другой чат (личный, Фаза 6)
    python notify.py --digest --days 14  сводка за другой срок
    python notify.py --check             проверить, что бот отвечает

Код возврата: 0 — всё в порядке, 1 — отправить не удалось, 2 — бот не настроен,
а отправлять было что. По нему расписание Фазы 5 понимает, дошло ли сообщение.
"""

import argparse
import html
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools"))

import yaml  # noqa: E402

import classify  # noqa: E402
import console  # noqa: E402
import diffing  # noqa: E402
import followup  # noqa: E402
import probe  # noqa: E402
import telegram  # noqa: E402
import wording  # noqa: E402
from robots import Robots  # noqa: E402

SNAPSHOTS = ROOT / "snapshots"
DIFFS = ROOT / "diffs"
NOTIFY = ROOT / "notify"
JOURNAL = NOTIFY / "journal.json"

# Слово детектора для находки, в которой ничего не произошло: текст тот же,
# изменился порядок строк или ушли метки шумодава. Пишется так же, как в
# detect.py, — это его слово, и расходиться им нельзя.
SHUFFLE = "только перестановка"

# Значения по умолчанию. Человек меняет их в config.yaml, раздел notify.
DEFAULTS = {
    "digest_days": 7,        # сколько дней попадает в недельную сводку
    "lines_in_alert": 6,     # сколько строк-улик показывать в срочном сообщении
    "items_in_digest": 5,    # сколько пунктов на одного конкурента в сводке
    "lines_in_digest_item": 3,  # сколько строк «появилось»/«исчезло» под пунктом
    "minor_in_digest": 10,   # сколько мелких появлений показывать отдельным списком
    "follow_links": True,    # дочитывать ли новость по ссылке перед отправкой
    "follow_lines": 6,       # сколько строк новости брать
    "follow_max": 2,         # сколько новостей дочитывать за один прогон
    "send_critical": True,   # слать ли критичное сразу; false — копить до сводки
}

# Как виды страниц называются в сообщении человеку. Слово «pricing» в алерте о
# цене выглядит как отладочный вывод, а не как сообщение владельцу.
PAGE_NAMES = {
    "home": "главная",
    "pricing": "тарифы",
    "blog": "блог и новости",
    "cases": "кейсы",
    "integrations": "интеграции",
    "extra": "ещё страница",
}

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


# Неделя обкатки (Фаза 6). Человек меняет это в config.yaml, раздел calibration.
CALIBRATION = {
    "mode": False,   # идёт ли обкатка: сообщения помечаются и уходят в личный чат
    "until": None,   # когда неделя заканчивается, ГГГГ-ММ-ДД
    "chat": None,    # чат обкатки; пусто — тот же, что в telegram_tokens.json
}

# Шапка сообщения на время обкатки. Одна строка сверху, дальше — ровно то
# сообщение, которое человек получил бы в боевом режиме: обкатка нужна, чтобы
# оценить настоящий текст, а не его облегчённый вид.
OBKATKA_HEAD = ("🧪 ОБКАТКА · это калибровка, а не боевая рассылка.\n"
                "Оцените: сигнал или шум. Разметка — python tools/calibrate.py")


def load_config() -> dict:
    path = ROOT / "config.yaml"
    if not path.exists():
        sys.exit("Не найден config.yaml рядом с notify.py.")
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {**DEFAULTS, **(config.get("notify") or {})}


def load_calibration() -> dict:
    """Настройки недели обкатки. Их читает не только notify.py — ещё calibrate.py."""
    path = ROOT / "config.yaml"
    if not path.exists():
        return dict(CALIBRATION)
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {**CALIBRATION, **(config.get("calibration") or {})}


def obkatka(text: str, cal: dict) -> str:
    """Пометить сообщение шапкой обкатки. В боевом режиме текст не трогается."""
    if not cal.get("mode") or not text:
        return text
    return OBKATKA_HEAD + "\n\n" + text


def obkatka_note(cal: dict, today: str) -> str | None:
    """Строка про обкатку, которую видно в журнале прогона. Или ничего.

    Зачем она. Обкатка — режим на неделю, и опаснее всего в нём не шум, а то,
    что про него забудут: радар останется помеченным «это не боевая рассылка»
    навсегда, и человек перестанет верить своим же сообщениям. Поэтому срок
    стоит в настройках, а не в голове, и после срока радар говорит об этом
    каждый день, пока человек не примет решение.
    """
    if not cal.get("mode"):
        return None
    until = str(cal.get("until") or "")
    if until and today > until:
        return ("🧪 обкатка: срок вышел " + ru_date(until) + ". Пора решать: порог, "
                "шумодав, источники — и переключать радар в боевой режим "
                "(calibration.mode: false). Что решать — в OBKATKA.md.")
    tail = f", до {ru_date(until)}" if until else ""
    return (f"🧪 обкатка{tail}: сообщения помечены и не считаются боевой рассылкой. "
            "Разметить находки — python tools/calibrate.py --razmetka")


def page_name(page: str) -> str:
    if page.startswith("telegram-"):
        return f"Telegram @{page[len('telegram-'):]}"
    if page.startswith("vk-"):
        return f"ВКонтакте {page[len('vk-'):]}"
    return PAGE_NAMES.get(page, page)


def plural(number: int, one: str, few: str, many: str) -> str:
    """«1 конкурента», «2 конкурентов», «5 конкурентов» — счёт по-русски.

    Мелочь, но сообщение читает владелец, а не программист: «Изменений: 1 у 1
    конкурентов» выглядит как недоделка и портит доверие ко всему остальному.
    """
    tail, hundred = number % 10, number % 100
    if tail == 1 and hundred != 11:
        return f"{number} {one}"
    if 2 <= tail <= 4 and not 12 <= hundred <= 14:
        return f"{number} {few}"
    return f"{number} {many}"


def ru_date(day: str) -> str:
    parsed = date.fromisoformat(day)
    return f"{parsed.day} {MONTHS[parsed.month - 1]}"


def ru_period(start: date, end: date) -> str:
    if start.month == end.month:
        return f"{start.day}–{end.day} {MONTHS[end.month - 1]}"
    return (f"{start.day} {MONTHS[start.month - 1]} — "
            f"{end.day} {MONTHS[end.month - 1]}")


# ─────────────────────────── чтение того, что нашёл детектор ───────────────────

def day_deltas(day: str) -> list[dict]:
    """Всё, что детектор нашёл за день, — включая то, что не дотянуло до порога.

    Читаются файлы дельт, а не сводка дня: в сводке лежит короткая выжимка, а
    классификатору нужны числа и полный список строк.

    Мелочь берётся сознательно, и это важное решение фазы. Порог в 120 символов
    отсекает незначительные правки по объёму, но срочность и объём — разные
    вещи. Строка «Гарантируем результат: не вырастет конверсия — вернём деньги»
    весит 75 символов и ниже порога; при этом гарантия результата стоит в
    таблице критичного первым десятком. То же и с ценой — Фаза 3 уже пропускает
    числа мимо порога по этой самой причине. Здесь правило то же, только шире:
    **порог решает, попадёт ли изменение в недельную сводку, а срочность
    решают правила.** Мелочь, которую правила не сочли критичной, не идёт
    никуда — ни в алерт, ни в сводку.
    """
    out = []
    for path in sorted(DIFFS.glob(f"*/*/{day}.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return sorted(out, key=lambda i: (i["конкурент"].lower(), i["страница"]))


def day_summary(day: str) -> dict:
    path = DIFFS / f"{day}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def snapshot_lines(domain: str, page: str, day: str) -> list[str] | None:
    """Строки снимка. Нужны правилу первого экрана — и только ему."""
    path = SNAPSHOTS / domain / page / f"{day}.txt"
    if not path.exists():
        return None
    return diffing.split_lines(path.read_text(encoding="utf-8"))


def numbers_moved(item: dict) -> bool:
    """Тронулись ли числа на странице — цена, число тарифов, размер скидки.

    Отдельная проверка нужна ровно в одном месте: решая, что находка пустая.
    Пустой её делает нетронутый текст, а число может смениться в строке, которую
    отсеял шумодав, — и тогда находка не пустая, какой бы пустой ни выглядела.
    """
    numbers = item.get("числа") or {}
    return bool(numbers.get("изменилось") or numbers.get("появилось")
                or numbers.get("исчезло"))


def sort_out(items: list[dict], rules: dict) -> tuple[list[dict], list[dict],
                                                      list[dict], list[dict]]:
    """Разложить находки дня на четыре стопки: срочное, в сводку, мелочь, пустое.

    Срочное — то, что признали критичным правила, независимо от объёма.
    В сводку — всё остальное, что детектор пропустил через порог.
    Мелочь — что порог не прошло и критичным не оказалось; она не идёт никуда,
    но её видно в отчёте дня, чтобы на калибровке Фазы 6 было что смотреть.
    Перестановка — не находка вовсе: текст тот же, изменился только порядок
    строк или ушли метки шумодава.

    Четвёртая стопка появилась в Фазе 6 и по её же правилу — на живых данных.
    В первом прогоне по расписанию 20.08.2026 таких строк оказалось тридцать
    семь из сорока одной, и в отчёте дня они лежали в мелочи: «мелочи 37»
    читалось как тридцать семь мелких правок у конкурентов, которых не было.
    Держать пустое в мелочи нельзя именно на калибровке: порог выбирается по
    числам из отчётов, а число, в котором сидит чужой мусор, даёт неверный порог.
    """
    critical, usual, minor, shuffled = [], [], [], []
    for item in items:
        if item.get("статус") == SHUFFLE and not numbers_moved(item):
            shuffled.append(item)
            continue
        verdict = classify.judge(
            item, rules,
            old_lines=snapshot_lines(item["домен"], item["страница"],
                                     item["сравнили со снимком"]),
            new_lines=snapshot_lines(item["домен"], item["страница"], item["дата"]))
        item["приговор"] = verdict
        if verdict.critical:
            critical.append(item)
        elif item.get("в дайджест"):
            usual.append(item)
        else:
            minor.append(item)
    return critical, usual, minor, shuffled


# ─────────────────────────── журнал: о чём уже писали ─────────────────────────

def read_journal() -> dict:
    if not JOURNAL.exists():
        return {"алерты": {}, "сводки": {}}
    try:
        saved = json.loads(JOURNAL.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Испорченный журнал — это не повод молчать. Хуже дубля только
        # пропущенный алерт, поэтому начинаем с чистого и пишем заново.
        return {"алерты": {}, "сводки": {}}
    saved.setdefault("алерты", {})
    saved.setdefault("сводки", {})
    return saved


def write_journal(journal: dict) -> None:
    NOTIFY.mkdir(parents=True, exist_ok=True)
    JOURNAL.write_text(json.dumps(journal, ensure_ascii=False, indent=2),
                       encoding="utf-8")


def alert_key(item: dict) -> str:
    return f"{item['домен']}/{item['страница']}/{item['дата']}"


# ─────────────────────────── сборка сообщений ─────────────────────────────────

def link(item: dict, text: str = "открыть страницу") -> str:
    url = item.get("адрес")
    if not url:
        return ""
    return f'<a href="{telegram.escape(url)}">{text}</a>'


def read_sources(items: list[dict], cfg: dict) -> None:
    """Дочитать новости, о которых собираемся написать прямо сейчас.

    Радар видит ленту новостей, а не сами новости: в ленте стоит заголовок и
    строка описания, а на что именно меняются цены, написано внутри. Владелец
    всё равно откроет ссылку и прочитает — значит, это надо сделать за него.

    Ходим только за срочным и не больше, чем сказано в config.yaml: обычное
    ждёт понедельника, и лишние запросы к чужому сайту ради него не нужны.
    Правила robots.txt соблюдаем те же, что и сборщик.
    """
    if not cfg.get("follow_links"):
        return
    robots = Robots(lambda u: probe.fetch(u, timeout=20, retries=1), probe.UA_BOT)
    read = 0
    for item in items:
        if read >= int(cfg["follow_max"]) or not item.get("адрес"):
            continue
        headlines = item["разница"].get("добавлено", [])[:4]
        if not headlines:
            continue
        try:
            found = followup.read(item["адрес"], headlines,
                                  int(cfg["follow_lines"]), robots)
        except Exception as error:            # сеть, разметка, кодировка
            # Дочитывание — приятное дополнение, а не условие отправки. Что бы
            # тут ни сломалось, срочное сообщение должно уйти.
            found = {"не прочитано": f"{type(error).__name__}: {error}"}
        if found:
            item["новость"] = found
            read += 1


def alert_text(day: str, items: list[dict], cfg: dict) -> str:
    """Срочное сообщение: что случилось прямо сейчас и где это посмотреть."""
    head = (f"⚡ <b>Радар: критичное</b> · {ru_date(day)}\n"
            + "Ждать до понедельника нельзя: "
            + plural(len(items), "изменение", "изменения", "изменений"))
    blocks = [head]

    for item in items:
        verdict = item["приговор"]
        lines = [f"<b>{telegram.escape(item['конкурент'])}</b> · "
                 f"{telegram.escape(page_name(item['страница']))}"]
        for reason in verdict.reasons:
            lines.append(f"— {telegram.escape(reason)}")
        for evidence in verdict.lines[:int(cfg["lines_in_alert"])]:
            lines.append(telegram.escape(evidence))
        if not item.get("в дайджест"):
            lines.append(f"<i>правка мелкая — "
                         f"{item['разница']['затронуто символов']} символов, "
                         f"но по смыслу срочная</i>")

        news = item.get("новость") or {}
        if news.get("строки"):
            since = news.get("с какого числа")
            lines.append("<b>Из новости"
                         + (f", {telegram.escape(since.lower())}" if since else "")
                         + ":</b>")
            # Первая строка новости обычно дословно повторяет описание из
            # ленты, которое уже процитировано выше как улика. Показывать её
            # второй раз — тратить экран телефона на то же самое.
            said = {" ".join(q.strip("«»").split()).lower() for q in verdict.lines}
            for line in news["строки"]:
                if " ".join(line.split()).lower() in said:
                    continue
                lines.append(f"  {telegram.escape(wording.shorten(line, 220))}")
            lines.append(f'<a href="{telegram.escape(news["адрес"])}">'
                         "читать новость</a>")
        elif news.get("не прочитано"):
            lines.append(f"<i>новость по ссылке дочитать не вышло: "
                         f"{telegram.escape(news['не прочитано'])}</i>")

        where = link(item)
        if where:
            lines.append(where)
        blocks.append("\n".join(lines))

    blocks.append("<i>Подробности — в файле дельты, полный список изменений за "
                  "день придёт в недельной сводке.</i>")
    return "\n\n".join(blocks)


def digest_text(start: date, end: date, by_competitor: dict, quiet: list[str],
                minor: list[dict], health: dict, stats: dict, cfg: dict) -> str:
    """Недельная сводка: всё, что накопилось, по одному конкуренту за раз."""
    changes = stats["изменений"]
    head = [f"📋 <b>Радар конкурентов</b> · {ru_period(start, end)}"]
    if changes:
        head.append(f"Изменений: {changes} у "
                    + plural(len(by_competitor), "конкурента", "конкурентов",
                             "конкурентов")
                    + (f". Срочных: {stats['критичных']} — их присылали сразу."
                       if stats["критичных"] else ". Срочных не было."))
    elif stats["дней с отчётом"] and minor:
        head.append("Изменений выше порога за неделю нет. Есть "
                    + plural(len(minor), "мелкая правка", "мелкие правки",
                             "мелких правок") + " — они ниже.")
    elif stats["дней с отчётом"]:
        head.append("За неделю у конкурентов не изменилось ничего. "
                    "Это тоже результат: проверено всё, что проверяется.")
    else:
        # Пустая сводка бывает двух разных видов, и путать их нельзя. «У
        # конкурентов тихо» — это измерение. «Радар не отработал ни дня» — это
        # сбой, и написать про него «тихо» значит соврать ровно в ту сторону,
        # в которую врать нельзя: человек решит, что за рынком следят.
        head.append("⚠️ Радар за эти дни не отработал ни разу — собирать сводку "
                    "не из чего. Это не тишина у конкурентов, это сбой сбора.")
    blocks = ["\n".join(head)]

    limit = int(cfg["items_in_digest"])
    depth = int(cfg["lines_in_digest_item"])
    for name in sorted(by_competitor, key=str.lower):
        rows = by_competitor[name]
        lines = [f"<b>{telegram.escape(name)}</b>"]
        for row in rows[:limit]:
            mark = "⚡ " if row["критично"] else ""
            why = f" — {telegram.escape(row['почему'])}" if row["почему"] else ""
            where = link(row["источник"], "ссылка")
            tail = f" · {where}" if where else ""
            lines.append(f"• {mark}{telegram.escape(page_name(row['страница']))}, "
                         f"{ru_date(row['дата'])}{why}{tail}")
            # Что именно появилось и что исчезло — самое ценное в сводке.
            # Одной строчкой «изменение на 377 символов» новую возможность
            # конкурента не разглядишь, а по ней иногда и стоит поторопиться.
            for line in row["появилось"][:depth]:
                lines.append(f"   + {telegram.escape(wording.shorten(line, 200))}")
            for line in row["исчезло"][:depth]:
                lines.append(f"   − {telegram.escape(wording.shorten(line, 200))}")
            hidden = (len(row["появилось"]) - depth) + (len(row["исчезло"]) - depth)
            if hidden > 0:
                lines.append(f"   <i>…и ещё {hidden} строк в дельте</i>")
        if len(rows) > limit:
            lines.append("<i>…и ещё "
                         + plural(len(rows) - limit, "изменение", "изменения",
                                  "изменений")
                         + ", смотреть в diffs/</i>")
        blocks.append("\n".join(lines))

    # Мелочь: то, что не дотянуло до порога и срочным не оказалось. В алерт
    # такое не идёт и отдельным пунктом сводки быть не заслуживает, но выкидывать
    # его совсем нельзя: короткая строка «Теперь с поддержкой WhatsApp» весит
    # тридцать символов и стоит дороже иной статьи в блоге.
    if minor:
        shown = minor[:int(cfg["minor_in_digest"])]
        rows = "\n".join(
            f"• {telegram.escape(item['конкурент'])} · "
            f"{telegram.escape(page_name(item['страница']))}: "
            f"{telegram.escape(wording.shorten(item['строка'], 160))}"
            for item in shown)
        more = (f"\n<i>…и ещё {len(minor) - len(shown)}</i>"
                if len(minor) > len(shown) else "")
        blocks.append("<b>По мелочи — появилось и исчезло</b> "
                      f"({len(minor)}), ниже порога, но вдруг пригодится:\n"
                      f"{rows}{more}")

    if quiet:
        blocks.append("<b>Ничего не менялось у:</b> "
                      + telegram.escape(", ".join(quiet)))

    # Здоровье сбора. В сводку идёт не список дневных сбоев, а ответ на вопрос
    # «что до сих пор не собирается». Сбой, который прошёл сам, изменение не
    # теряет: назавтра источник снимается, и детектор сравнивает с последним
    # снимком, каким бы днём тот ни был. Такие сбои сворачиваются в одну строку.
    broken = health["не собирается"]
    if broken:
        rows = "\n".join(f"• {telegram.escape(line)}" for line in broken[:10])
        more = f"\n<i>…и ещё {len(broken) - 10}</i>" if len(broken) > 10 else ""
        blocks.append(f"⚠️ <b>Не собирается</b> ({len(broken)}) — тут радар "
                      f"молчит не потому, что тихо:\n{rows}{more}")
    if health["прошли сами"]:
        blocks.append("<i>Разовых сбоев сбора за неделю: "
                      f"{health['прошли сами']}. Все эти источники сняты в "
                      "следующие дни, изменения не потеряны.</i>")

    if health["смотрим глазами"]:
        blocks.append("<i>Автоматически не берутся и ждут ваших глаз: "
                      + telegram.escape(", ".join(health["смотрим глазами"]))
                      + ". Смотреть раз в месяц.</i>")

    missed = stats["дней"] - stats["дней с отчётом"]
    tail = (f"<i>Дней в сводке: {stats['дней']}, из них радар отработал "
            f"{stats['дней с отчётом']}.</i>")
    if missed and stats["дней с отчётом"]:
        tail += ("\n⚠️ <i>За остальные "
                 + plural(missed, "день", "дня", "дней")
                 + " разбора нет: в эти сутки радар не запускался, "
                 "и что менялось у конкурентов — неизвестно.</i>")
    blocks.append(tail)
    return "\n\n".join(blocks)


def summarize_change(item: dict) -> str:
    """Одна строка про изменение — то, что человек прочитает в сводке."""
    verdict = item.get("приговор")
    if verdict and verdict.reasons:
        return verdict.reasons[0]
    for line in item.get("кратко", []):
        if line.startswith("+ "):
            return line[2:]
    for line in item.get("кратко", []):
        if line.startswith("− "):
            return "убрано: " + line[2:]
    return f"изменение на {item['разница']['затронуто символов']} символов"


# ─────────────────────────── отправка ─────────────────────────────────────────

def deliver(bot: telegram.Bot | None, text: str, chat: str | None,
            dry_run: bool, silent: bool = False) -> tuple[bool, str, list[int]]:
    """Отправить или показать. Возвращает «дошло ли», причину и номера сообщений."""
    if dry_run:
        return False, "холостой запуск: ничего не отправлено", []
    if bot is None:
        return False, "бот не настроен: сообщение никуда не ушло", []
    try:
        ids = bot.send(text, chat_id=chat, silent=silent)
    except telegram.TelegramError as error:
        return False, f"Telegram не принял сообщение — {error}", []
    return True, f"отправлено в чат {chat or bot.chat_id}", ids


def show(text: str) -> None:
    """Показать сообщение на экране так, как его увидит человек в Telegram.

    Разметка снимается, а не печатается: неделя обкатки Фазы 6 проходит без
    боевой рассылки, и сообщения читают именно с экрана. Если на экране будет
    HTML, читать станут его, а не сообщение.
    """
    plain = re.sub(r"</?[bi]>", "", text)
    plain = re.sub(r'<a href="([^"]+)">([^<]+)</a>', r"\2: \1", plain)
    for line in html.unescape(plain).splitlines():
        print("   ", wording.shorten(line, 200))


# ─────────────────────────── ежедневный проход ────────────────────────────────

def run_day(day: str, cfg: dict, rules: dict, bot, args) -> int:
    items = day_deltas(day)
    critical, usual, minor, shuffled = sort_out(items, rules)
    journal = read_journal()

    fresh = [i for i in critical if args.resend or alert_key(i) not in journal["алерты"]]
    fresh_keys = {alert_key(i) for i in fresh}
    again = len(critical) - len(fresh)

    empty = f", пустых {len(shuffled)}" if shuffled else ""
    print(f"Разбор за {day}. Находок: {len(items) - len(shuffled)} — срочных "
          f"{len(critical)}, в сводку {len(usual)}, мелочи {len(minor)}{empty}.")
    for item in critical:
        mark = "КРИТИЧНО" if alert_key(item) in fresh_keys else "уже присылали"
        print(f"  {mark:<14} {item['конкурент']} · {page_name(item['страница'])}: "
              f"{'; '.join(item['приговор'].reasons)}")
        for line in item["приговор"].lines[:int(cfg["lines_in_alert"])]:
            print(f"                 {wording.shorten(line, 150)}")
    for item in usual:
        print(f"  {'в сводку':<14} {item['конкурент']} · {page_name(item['страница'])}: "
              f"{wording.shorten(summarize_change(item), 110)}")

    for item in critical + usual + minor:
        for note in item["приговор"].notes:
            print(f"  ! {item['конкурент']} · {item['страница']}: {note}")

    sent, why, ids = False, "критичного нет — молчим", []
    text = ""
    if fresh and not cfg["send_critical"]:
        why = "send_critical выключен в config.yaml: критичное уйдёт в сводке"
    elif fresh:
        read_sources(fresh, cfg)
        text = obkatka(alert_text(day, fresh, cfg), load_calibration())
        print(f"\nСрочное сообщение ({len(text)} символов):")
        show(text)
        sent, why, ids = deliver(bot, text, args.to, args.dry_run)
        print(f"\n{why}")
        if sent:
            when = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for item in fresh:
                journal["алерты"][alert_key(item)] = {
                    "когда": when,
                    "конкурент": item["конкурент"],
                    "страница": item["страница"],
                    "правила": item["приговор"].rules,
                    "чат": args.to or (bot.chat_id if bot else None),
                    "сообщения": ids,
                }
            write_journal(journal)
    elif again:
        why = f"критичное есть ({again}), но о нём уже писали — молчим"
        print(f"\n{why}")
    else:
        print(f"\n{why}")

    report = {
        "дата": day,
        "разобрано": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "бот": bot.masked if bot else None,
        "чат": args.to or (bot.chat_id if bot else None),
        "итоги": {"критично": len(critical), "обычно": len(usual),
                  "мелочь": len(minor), "перестановка": len(shuffled),
                  "отправлено": len(fresh) if sent else 0,
                  "уже присылали": again},
        "критично": [_row(i, sent) for i in critical],
        "обычно": [_row(i, False) for i in usual],
        "мелочь": [f"{i['конкурент']} · {i['страница']}: "
                   f"{i['разница']['затронуто символов']} символов" for i in minor],
        "отправка": why,
        "сообщение": text,
    }
    if not args.dry_run:
        NOTIFY.mkdir(parents=True, exist_ok=True)
        (NOTIFY / f"{day}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Отчёт: notify/{day}.json")
    else:
        print("Это был холостой запуск: ничего не отправлено и не записано.")

    if fresh and not sent and not args.dry_run and cfg["send_critical"]:
        return 2 if bot is None else 1
    return 0


def _row(item: dict, sent: bool) -> dict:
    verdict = item["приговор"]
    out = {
        "конкурент": item["конкурент"],
        "страница": item["страница"],
        "адрес": item["адрес"],
        **verdict.to_dict(),
        "улики": verdict.lines,
        "дельта": f"diffs/{item['домен']}/{item['страница']}/{item['дата']}.json",
    }
    if item.get("новость"):
        out["новость"] = item["новость"]
    if verdict.critical:
        out["отправлено"] = sent
    return out


# ─────────────────────────── недельная сводка ─────────────────────────────────

def run_digest(day: str, cfg: dict, rules: dict, bot, args) -> int:
    """Сводка за неделю, заканчивающуюся вчерашним днём.

    Почему вчерашним: сводку шлют утром понедельника, и сегодняшний сбор к тому
    моменту либо ещё не прошёл, либо прошёл час назад. Неделя «понедельник —
    воскресенье» и человеку понятнее, и не зависит от того, в каком порядке
    расписание запустит сбор и рассылку.
    """
    end = date.fromisoformat(day) - timedelta(days=1)
    start = end - timedelta(days=int(args.days or cfg["digest_days"]) - 1)
    depth = int(cfg["lines_in_digest_item"])

    by_competitor: dict[str, list[dict]] = {}
    small: list[dict] = []
    quiet_days: list[set[str]] = []
    changed_names: set[str] = set()
    fails: dict[str, list[tuple[str, str]]] = {}
    last_ok: dict[str, str] = {}
    by_hand: dict[str, str] = {}
    days_with_report = 0
    critical_count = 0
    changes = 0

    current = start
    while current <= end:
        stamp = current.isoformat()
        summary = day_summary(stamp)
        if summary:
            days_with_report += 1
            quiet_days.append(set(summary.get("ничего не менялось у", [])))
            for line in summary.get("не проверено", []):
                key, _, note = line.partition(": ")
                fails.setdefault(key, []).append((stamp, note))
            for line in (summary.get("без изменений", [])
                         + summary.get("точка отсчёта", [])):
                last_ok[line] = stamp
            for line in summary.get("смотрим глазами", []):
                who, _, why = line.partition(": ")
                by_hand[who] = why

        items = day_deltas(stamp)
        critical, usual, minor, shuffled = sort_out(items, rules)
        critical_count += len(critical)
        changes += len(critical) + len(usual)

        for item in critical + usual + minor + shuffled:
            last_ok[f"{item['конкурент']} · {item['страница']}"] = stamp
        for item in critical + usual:
            changed_names.add(item["конкурент"])
            verdict = item["приговор"]
            by_competitor.setdefault(item["конкурент"], []).append({
                "дата": stamp,
                "страница": item["страница"],
                "почему": verdict.reasons[0] if verdict.reasons else "",
                "появилось": item["разница"].get("добавлено", []),
                "исчезло": item["разница"].get("удалено", []),
                "критично": verdict.critical,
                "источник": item,
            })
        for item in minor:
            line = _first_line(item)
            if line:
                small.append({"дата": stamp, "конкурент": item["конкурент"],
                              "страница": item["страница"], "строка": line})
        current += timedelta(days=1)

    # «Ничего не менялось у …» — про тех, кого за неделю хоть раз проверили и
    # у кого при этом не нашлось ни одного изменения. Мёртвый сайт и источник
    # под капчей сюда не попадают: их не проверяли, и говорить «у них тихо»
    # было бы неправдой.
    checked = set().union(*quiet_days) if quiet_days else set()
    quiet = sorted(checked - changed_names, key=str.lower)

    health = _health(fails, last_ok, by_hand)
    stats = {"изменений": changes, "критичных": critical_count,
             "дней": (end - start).days + 1, "дней с отчётом": days_with_report}

    text = obkatka(
        digest_text(start, end, by_competitor, quiet, small, health, stats, cfg),
        load_calibration())
    print(f"Сводка за {start.isoformat()} — {end.isoformat()}: "
          + plural(changes, "изменение", "изменения", "изменений") + " у "
          + plural(len(by_competitor), "конкурента", "конкурентов", "конкурентов")
          + f", срочных {critical_count}, по мелочи {len(small)}.")
    if health["не собирается"]:
        print("Не собирается до сих пор:")
        for line in health["не собирается"]:
            print("  •", line)
    print(f"\nСообщение ({len(text)} символов, "
          f"частей {len(telegram.split(text))}):")
    show(text)

    journal = read_journal()
    if not args.resend and end.isoformat() in journal["сводки"]:
        print("\nСводка за этот период уже отправлялась. "
              "Повторить — ключ --resend.")
        return 0

    sent, why, ids = deliver(bot, text, args.to, args.dry_run)
    print(f"\n{why}")

    if sent:
        journal["сводки"][end.isoformat()] = {
            "когда": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "период": f"{start.isoformat()}—{end.isoformat()}",
            "изменений": changes,
            "критичных": critical_count,
            "чат": args.to or (bot.chat_id if bot else None),
            "сообщения": ids,
        }
        write_journal(journal)

    report = {
        "период": f"{start.isoformat()}—{end.isoformat()}",
        "собрано": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "итоги": {**stats, "по мелочи": len(small)},
        "по конкурентам": {name: [{k: v for k, v in row.items() if k != "источник"}
                                  for row in rows]
                           for name, rows in by_competitor.items()},
        "по мелочи": small,
        "ничего не менялось у": quiet,
        "здоровье сбора": health,
        "отправка": why,
        "сообщение": text,
    }
    if not args.dry_run:
        NOTIFY.mkdir(parents=True, exist_ok=True)
        (NOTIFY / f"digest-{end.isoformat()}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Отчёт: notify/digest-{end.isoformat()}.json")
    else:
        print("Это был холостой запуск: ничего не отправлено и не записано.")

    if not sent and not args.dry_run:
        return 2 if bot is None else 1
    return 0


def _first_line(item: dict) -> str:
    """Одна строка про мелкую правку: что появилось, а если нечего — что ушло."""
    for line in item["разница"].get("добавлено", []):
        return f"+ {line}"
    for line in item["разница"].get("удалено", []):
        return f"− {line}"
    return ""


def _health(fails: dict, last_ok: dict, by_hand: dict) -> dict:
    """Здоровье сбора за период: что молчит до сих пор, а что уже починилось.

    Сбой сбора сам по себе в сводку не нужен. Если источник не снялся во
    вторник, а в среду снялся, изменение не потеряно: детектор сравнивает
    свежий снимок с последним имеющимся, каким бы днём тот ни был, — у него для
    этого и заведено поле «разрыв в днях». К понедельнику такой сбой уже
    неинтересен, и место в сводке ему не нужно.

    А вот источник, который не собирается до сих пор, — совсем другое дело:
    про него радар молчит не потому, что у конкурента тихо, а потому, что не
    смотрел. Это и попадает в сводку поимённо, с датой начала молчания.
    """
    broken, healed = [], 0
    for key, events in sorted(fails.items()):
        good = last_ok.get(key)
        after = [(day, note) for day, note in events if good is None or day > good]
        healed += len(events) - len(after)
        if not after:
            continue
        who, _, page = key.partition(" · ")
        since = ru_date(min(day for day, _ in after))
        note = max(after)[1]
        broken.append(f"{who} · {page_name(page)} — не собирается с {since} ({note})")
    return {"не собирается": broken, "прошли сами": healed,
            "смотрим глазами": sorted(by_hand)}


# ─────────────────────────── запуск ───────────────────────────────────────────

def main() -> int:
    console.setup()
    ap = argparse.ArgumentParser(description="Классификация и уведомления радара")
    ap.add_argument("--date", help="разбирать этот день (по умолчанию сегодня)")
    ap.add_argument("--digest", action="store_true",
                    help="собрать и отправить недельную сводку")
    ap.add_argument("--days", type=int, help="сколько дней берёт сводка")
    ap.add_argument("--to", help="номер другого чата — например, личного")
    ap.add_argument("--resend", action="store_true",
                    help="отправить заново, не глядя в журнал")
    ap.add_argument("--dry-run", action="store_true",
                    help="показать сообщение, ничего не отправлять и не писать")
    ap.add_argument("--check", action="store_true",
                    help="проверить, что бот отвечает, и выйти")
    args = ap.parse_args()

    cfg = load_config()
    cal = load_calibration()
    rules = classify.load_rules(ROOT / "rules.yaml")
    day = args.date or date.today().isoformat()

    # Чат обкатки. Смысл в том, чтобы неделю калибровки сообщения шли туда, где
    # их читает один человек и оценивает, а не туда, куда радар будет писать
    # в боевом режиме. Заданный руками --to сильнее настройки: он и нужен для
    # разовой отправки в другое место.
    if cal["mode"] and cal["chat"] and not args.to:
        args.to = str(cal["chat"])

    try:
        bot = telegram.load(ROOT)
    except telegram.TelegramError as error:
        print(f"Доступ к боту есть, но настроен не до конца: {error}")
        return 2

    if args.check:
        if bot is None:
            print("Бот не настроен. Как его завести — в TELEGRAM-BOT.md.")
            return 2
        try:
            who = bot.me()
        except telegram.TelegramError as error:
            print(f"Бот не отвечает: {error}")
            return 1
        print(f"Бот на связи: @{who.get('username')} ({who.get('first_name')}), "
              f"токен {bot.masked}, доступ из: {bot.source}, чат {bot.chat_id}.")
        return 0

    if bot is None:
        print("Бот не настроен — сообщения будут показаны на экране "
              "(как в неделю обкатки Фазы 6). Завести бота — TELEGRAM-BOT.md.\n")

    note = obkatka_note(cal, day)
    if note:
        print(note + "\n")

    if args.digest:
        return run_digest(day, cfg, rules, bot, args)
    return run_day(day, cfg, rules, bot, args)


if __name__ == "__main__":
    sys.exit(main())
