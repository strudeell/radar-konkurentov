#!/usr/bin/env python3
"""Проверка калибровки — Фаза 6.

Отвечает на вопрос «а правильно ли программа калибровки считает то, по чему
человек будет менять порог». Вопрос не праздный: в конце недели обкатки её
таблица — единственное основание для решений, а сама она в этот момент проверена
ровно одним прогоном на пяти находках, где все вердикты стоят вопросительным
знаком.

Ждать неделю ради проверки нельзя — за эту неделю радар будет работать с
непроверенным расчётом. Поэтому здесь тот же приём, что в
[schedule_check.py](schedule_check.py): находки выдуманные, а разбор настоящий.
Программе подкладываются случаи, каких за два дня наблюдений не встретилось —
сигнал ниже порога, шум выше порога, мигающая строка, один пост двумя
каналами, — и проверяется, что она говорит про них то, что должна.

Один случай не выдуманный: лист разметки берётся настоящий, из накопленного, и
проверяется главное его свойство — перезапись не теряет проставленных человеком
слов. Свойство некрасивое на вид и дорогое по цене ошибки: разметка недели
делается один раз и восстановлению не подлежит.

Запуск:

    python tools/calibrate_check.py
"""

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import yaml  # noqa: E402

import calibrate  # noqa: E402
import classify  # noqa: E402
import console  # noqa: E402

CFG = {"min_changed_chars": 120, "numbers_ignore_threshold": True}

cases = 0
failed = 0


def check(name: str, expected, got, *notes: str) -> None:
    """Один случай: чего ждали, что вышло. Расхождение — повод остановиться."""
    global cases, failed
    cases += 1
    ok = expected == got
    failed += 0 if ok else 1
    print(f"\n  {'как ожидали' if ok else 'РАЗОШЛОСЬ  '} {name}")
    print(f"               ждали {expected}, вышло {got}")
    for note in notes:
        print(f"               • {note}")


def make(day: str, domain: str, page: str, chars: int, *, klass: str = "мелочь",
         numbers: bool = False, added: list[str] | None = None,
         removed: list[str] | None = None, who: str = "Кто-то") -> dict:
    """Находка такого же вида, какой её кладёт gather() из настоящих дельт."""
    return {
        "день": day,
        "ключ": f"{domain}|{page}",
        "имя": f"{who} · {page}",
        "конкурент": who,
        "домен": domain,
        "страница": page,
        "адрес": f"https://{domain}/",
        "класс": klass,
        "объём": chars,
        "числа": numbers,
        "канал": classify.page_kind(page) == classify.CHANNEL_PAGE,
        "правила": [],
        "строки": (added or [])[:3],
        "добавлено": added or [],
        "удалено": removed or [],
    }


def marks_for(pairs: list[tuple[dict, str]]) -> dict:
    return {(item["день"], item["ключ"]): {"вердикт": verdict, "заметка": ""}
            for item, verdict in pairs}


def row(table: list[dict], threshold: int) -> dict:
    return next(line for line in table if line["порог"] == threshold)


# ─────────────────────────── порог ────────────────────────────────────────────

def case_signal_below_threshold() -> None:
    """Сигнал в 80 символов: порог 120 его теряет, порог 60 — нет.

    Это главный случай всей фазы. «Теперь с поддержкой WhatsApp» — тридцать
    символов и целая новая возможность у конкурента; если такие строки уходят
    в никуда, поднимать порог нельзя, как бы ни хотелось тишины.
    """
    item = make("2026-08-21", "example.ru", "home", 80)
    table = calibrate.sweep([item], marks_for([(item, "сигнал")]), CFG)
    check("сигнал ниже порога: порог 120 его теряет",
          1, row(table, 120)["потеряно сигналов"],
          "80 символов — «Теперь с поддержкой WhatsApp» и есть",
          "потерянный сигнал дороже лишней строки в сводке")
    check("тот же сигнал при пороге 60 доходит",
          (1, 0), (row(table, 60)["дошло"], row(table, 60)["потеряно сигналов"]))


def case_noise_above_threshold() -> None:
    """Шум в 400 символов: порог 120 его пропускает, порог 800 — нет."""
    item = make("2026-08-21", "example.ru", "blog", 400)
    table = calibrate.sweep([item], marks_for([(item, "шум")]), CFG)
    check("шум выше порога: при 120 доходит до человека",
          1, row(table, 120)["шум"],
          "ради таких находок порог и поднимают")
    check("при пороге 800 тот же шум не доходит",
          (0, 0), (row(table, 800)["дошло"], row(table, 800)["потеряно сигналов"]))


def case_price_ignores_threshold() -> None:
    """Цена в восемь символов доходит при любом пороге.

    Решение Фазы 3: числа на коммерческих страницах порогом не отсекаются.
    Калибровка не имеет права его отменять — иначе первый же поднятый порог
    похоронит главный сигнал системы.
    """
    item = make("2026-08-21", "example.ru", "pricing", 8, numbers=True)
    table = calibrate.sweep([item], marks_for([(item, "сигнал")]), CFG)
    delivered = {line["порог"]: line["дошло"] for line in table}
    check("цена в 8 символов доходит при всех порогах",
          {step: 1 for step in delivered}, delivered,
          "«49 000 ₽» → «54 000 ₽» задевает восемь символов")


def case_critical_ignores_threshold() -> None:
    """Критичное не зависит от порога: его судьбу решают правила, а не объём."""
    item = make("2026-08-21", "example.ru", "home", 20, klass="критично")
    table = calibrate.sweep([item], marks_for([(item, "сигнал")]), CFG)
    check("критичное доходит и при пороге 800",
          1, row(table, 800)["дошло"],
          "слова рынка из config/rules.yaml проходят мимо порога")


def case_unmarked_counted_apart() -> None:
    """Неразмеченное не считается ни сигналом, ни шумом.

    Иначе неполная разметка выглядела бы как чистая работа радара: сорок
    находок, шума ноль. Ноль здесь означал бы «не смотрели», а прочитался бы
    как «шума не было».
    """
    items = [make("2026-08-21", "a.ru", "home", 300),
             make("2026-08-21", "b.ru", "home", 300)]
    table = calibrate.sweep(items, marks_for([(items[0], "сигнал")]), CFG)
    line = row(table, 120)
    check("одна размечена, вторая нет — считаются отдельно",
          (1, 0, 1), (line["сигнал"], line["шум"], line["?"]))


# ─────────────────────────── шумодав и двойники ───────────────────────────────

def case_flapping_line() -> None:
    """Строка, которая то появляется, то исчезает, — кандидат в шумодав."""
    flap = "Оставьте заявку — перезвоним за 15 минут"
    items = [make("2026-08-21", "example.ru", "home", 300, added=[flap]),
             make("2026-08-22", "example.ru", "home", 300, removed=[flap])]
    found = calibrate.candidates(items)
    check("мигающая строка попала в кандидаты",
          (1, True), (len(found), found[0]["мигает"] if found else None),
          "конкурент так не делает — так делает разметка страницы")


def case_single_line_is_not_noise() -> None:
    """Строка, встретившаяся один раз, кандидатом не считается.

    Новая статья в блоге появляется ровно один раз и больше не исчезает. Если
    такие строки попадут в шумодав, радар ослепнет на самое обычное изменение.
    """
    once = "Кейс: как мы сократили время разбора звонка втрое"
    items = [make("2026-08-21", "example.ru", "blog", 300, added=[once])]
    check("одиночная строка в кандидаты не идёт",
          0, len(calibrate.candidates(items)))


def case_twins() -> None:
    """Один пост в Telegram и во ВКонтакте — одна новость, а не две."""
    post = ("🔗 Партнёрство, которое уже включено в вашем аккаунте. "
            "Спойлер: настраивать ничего не нужно, ссылка уже в кабинете")
    items = [make("2026-08-21", "example.ru", "telegram-example", 900,
                  added=[f"[1929] 2026-08-20 07:45 · {post} — читайте в блоге"],
                  who="Кто-то"),
             make("2026-08-21", "example.ru", "vk-example", 900,
                  added=[f"[9009] 2026-08-20 07:47 · {post} #партнёрство"],
                  who="Кто-то")]
    found = calibrate.twins(items)
    check("один пост двумя каналами найден как двойник",
          1, len(found),
          "номера постов и время разные, начало текста одно")


def case_different_posts_are_not_twins() -> None:
    """Разные посты в двух каналах двойниками не считаются."""
    items = [make("2026-08-21", "example.ru", "telegram-example", 900,
                  added=["[1929] 2026-08-20 07:45 · Вебинар про речевую аналитику "
                         "в отделе продаж, запись будет"]),
             make("2026-08-21", "example.ru", "vk-example", 900,
                  added=["[9009] 2026-08-20 15:40 · Обновили тарифы: теперь минуты "
                         "считаются пакетами по тысяче"])]
    check("разные посты двойниками не признаны", 0, len(calibrate.twins(items)))


# ─────────────────────────── вывод программы ──────────────────────────────────

def advice(findings: list[dict], marks: dict, *, worked: int = 7,
           days: int = 7, enough: int = 1) -> str:
    """Совет программы одной строкой.

    Планка достаточности здесь временно опускается: иначе случай проверял бы не
    арифметику совета, а саму планку. На планку есть два отдельных случая ниже.
    """
    real = calibrate.ENOUGH_MARKED
    calibrate.ENOUGH_MARKED = enough
    try:
        counted = Counter(calibrate.verdict_of(item, marks) for item in findings)
        return " ".join(calibrate.recommend(findings, marks, CFG, counted,
                                            worked, days))
    finally:
        calibrate.ENOUGH_MARKED = real


def case_advice_lower() -> None:
    """Если порог теряет размеченный сигнал — программа советует его опустить."""
    item = make("2026-08-21", "example.ru", "home", 80)
    marks = marks_for([(item, "сигнал")])
    text = advice([item], marks)
    check("совет при потерянном сигнале — опустить порог",
          True, "Опустить" in text, text.strip().split("\n")[0][:90])


def case_advice_raise() -> None:
    """Если шум отсекается выше без потери сигналов — советует поднять."""
    noise = make("2026-08-21", "a.ru", "blog", 200)
    signal = make("2026-08-21", "b.ru", "pricing", 900)
    marks = marks_for([(noise, "шум"), (signal, "сигнал")])
    text = advice([noise, signal], marks)
    check("совет при отсекаемом шуме — поднять порог",
          True, "поднять" in text, text.strip().split("\n")[0][:90])


def case_advice_without_marks() -> None:
    """Без разметки программа не советует ничего и говорит об этом прямо."""
    item = make("2026-08-21", "example.ru", "home", 300)
    text = advice([item], {})
    check("без разметки вывода нет", True, "не по чему" in text,
          "молчание тут честнее таблицы")


def case_thin_week_blocks_advice() -> None:
    """Неделя, в которую радар отработал два дня, порога не выбирает.

    Случай, ради которого планка и появилась. Живая разметка первых двух дней
    дала пять находок, и расчёт с полной уверенностью посоветовал поднять порог
    со 120 до 800 — по одной шумной находке. Порог, подогнанный под два дня,
    хуже догадки из плана: у него вид измерения.
    """
    noise = make("2026-08-21", "a.ru", "blog", 200)
    signal = make("2026-08-21", "b.ru", "pricing", 900)
    marks = marks_for([(noise, "шум"), (signal, "сигнал")])
    text = advice([noise, signal], marks, worked=2, days=7,
                  enough=calibrate.ENOUGH_MARKED)
    check("два отработанных дня из семи — совет заблокирован",
          (True, True), ("менять рано" in text, "продлить обкатку" in text),
          "предварительную картину программа всё равно показывает")


def case_few_marks_block_advice() -> None:
    """Полная неделя, но три размеченные находки — тоже мало."""
    items = [make("2026-08-21", "a.ru", "blog", 200),
             make("2026-08-22", "b.ru", "blog", 300),
             make("2026-08-23", "c.ru", "blog", 400)]
    marks = marks_for([(items[0], "шум"), (items[1], "шум"), (items[2], "сигнал")])
    text = advice(items, marks, worked=7, days=7, enough=calibrate.ENOUGH_MARKED)
    check("семь дней, но три находки — совет заблокирован",
          True, "менять рано" in text,
          "одна находка сдвигает совет на сотни символов")


# ─────────────────────────── лист разметки ────────────────────────────────────

def case_sheet_keeps_verdicts(tmp: Path) -> None:
    """Перезапись листа не теряет проставленных слов. Случай на живых данных."""
    real_calib, real_sheet = calibrate.CALIB, calibrate.SHEET
    calibrate.CALIB, calibrate.SHEET = tmp, tmp / "razmetka.yaml"
    try:
        rules = classify.load_rules(ROOT / "config" / "rules.yaml")
        findings, _ = calibrate.gather(calibrate.period(14, None), rules)
        if not findings:
            print("\n  пропущено   лист разметки: находок пока нет")
            return
        calibrate.write_sheet(findings, {}, dry=False)

        raw = yaml.safe_load(calibrate.SHEET.read_text(encoding="utf-8"))
        day = sorted(raw)[0]
        raw[day][0]["вердикт"] = "шум"
        raw[day][0]["заметка"] = "проверка"
        calibrate.SHEET.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
            encoding="utf-8")

        marks = calibrate.load_sheet()
        added, total = calibrate.write_sheet(findings, marks, dry=False)
        again = yaml.safe_load(calibrate.SHEET.read_text(encoding="utf-8"))[day][0]
        check("перезапись листа не теряет проставленного",
              ("шум", "проверка", 0), (again["вердикт"], again["заметка"], added),
              f"находок в листе: {total}, из них новых: {added}",
              "разметка недели делается один раз и восстановлению не подлежит")
    finally:
        calibrate.CALIB, calibrate.SHEET = real_calib, real_sheet


def main() -> int:
    console.setup()
    print("Проверка калибровки на выдуманных находках и настоящем разборе.")

    case_signal_below_threshold()
    case_noise_above_threshold()
    case_price_ignores_threshold()
    case_critical_ignores_threshold()
    case_unmarked_counted_apart()
    case_flapping_line()
    case_single_line_is_not_noise()
    case_twins()
    case_different_posts_are_not_twins()
    case_advice_lower()
    case_advice_raise()
    case_advice_without_marks()
    case_thin_week_blocks_advice()
    case_few_marks_block_advice()

    tmp = ROOT / "work" / "calibration" / "_proverka"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        case_sheet_keeps_verdicts(tmp)
    finally:
        for path in tmp.glob("*"):
            path.unlink()
        tmp.rmdir()

    print(f"\nСлучаев: {cases}, разошлось с ожиданием: {failed}.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
