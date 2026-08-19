#!/usr/bin/env python3
"""Классификатор срочности — критично или обычно.

Договорённость с заказчиком: обычные изменения уходят одной недельной сводкой
по понедельникам, критичные проверяются каждый день и отправляются сразу, как
только нашлись. Значит, вся система стоит на одном ответе — «это критично или
нет», и ответ должен быть объясним. Поэтому здесь не оценка важности числом, а
набор правил, каждое из которых умеет сказать человеку, почему оно сработало:
не «важность 0.87», а «цена: было 49 000 → стало 54 000 ₽/мес».

Откуда берутся правила. Таблица двух классов лежит в 03-chto-otslezhivat.md,
слова рынка — в rules.yaml рядом с этим файлом. В коде остаётся только то, что
от рынка не зависит.

Три источника критичности, в порядке надёжности.

**Числа на коммерческих страницах.** Самый твёрдый сигнал: его нашёл разбор цен
Фазы 3, он не зависит от формулировок и работает одинаково на всех семнадцати
страницах. Изменилось число — цена поменялась. Число исчезло или появилось —
скорее всего, тариф убрали или добавили.

**Первый экран главной.** Верх главной страницы — это сжатое позиционирование
конкурента. Изменился он — игрок развернулся. Проверяется по месту строки в
снимке, а не по словам: если добавленная строка стоит в первых содержательных
строках сегодняшнего снимка (или удалённая стояла в них вчера), правило
срабатывает. Почему «содержательных», а не «первых подряд» — в rules.yaml,
там же лежит замер по семнадцати главным страницам.

**Слова из словаря.** Гарантия результата, бесплатный тариф, коробочная версия,
лимит по менеджерам, объявление о смене тарифов. Это самая хрупкая часть: слова
конкурент пишет как хочет. Словарь и живёт отдельным файлом, чтобы неделя
обкатки Фазы 6 правила меняла, а программу не трогала.

Чего классификатор сознательно не делает: он не смотрит на объём изменения.
Объём уже отработал в Фазе 3 порогом в 120 символов — то, что до классификатора
дошло, изменением уже признано. Смена цены задевает шестнадцать символов и при
этом критична, длинная статья в блоге задевает три тысячи и остаётся обычной.
Размер тут ничего не говорит о срочности.
"""

import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

import prices  # noqa: E402

CRITICAL = "критично"
NORMAL = "обычно"

# Виды страниц, которые в правилах называются одним словом «канал»: посты в
# Telegram и ВКонтакте. В sources.yaml они зовутся telegram-<id> и vk-<id>,
# и перечислять их поимённо в словаре бессмысленно.
CHANNEL_KINDS = ("telegram-", "vk-")

ANY_PAGE = "любая"
CHANNEL_PAGE = "канал"

# Сколько символов строки-улики показываем в сообщении. Длиннее — сообщение
# перестаёт читаться с телефона, а полная строка всегда есть в файле дельты.
QUOTE = 160

DEFAULTS = {
    "first_screen": {"lines": 6, "min_chars": 25, "pages": ["home"],
                     "say": "изменился первый экран главной страницы"},
    "numbers": {"in_message": 6, "flood": 40},
    "keywords": [],
}


@dataclass
class Verdict:
    """Приговор по одному изменению: срочно или подождёт, и почему."""
    critical: bool = False
    reasons: list[str] = field(default_factory=list)   # почему, человеческим языком
    rules: list[str] = field(default_factory=list)     # какие правила сработали
    lines: list[str] = field(default_factory=list)     # улики для сообщения
    notes: list[str] = field(default_factory=list)     # что проверить не вышло

    @property
    def label(self) -> str:
        return CRITICAL if self.critical else NORMAL

    def add(self, rule: str, reason: str, evidence: list[str] | None = None) -> None:
        self.critical = True
        if rule not in self.rules:
            self.rules.append(rule)
        if reason not in self.reasons:
            self.reasons.append(reason)
        for line in evidence or []:
            if line not in self.lines:
                self.lines.append(line)

    def to_dict(self) -> dict:
        out = {"класс": self.label, "почему": self.reasons, "правила": self.rules}
        if self.notes:
            out["не проверено"] = self.notes
        return out


def load_rules(path: Path) -> dict:
    """Словарь из rules.yaml с уже скомпилированными регулярками."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rules = {**DEFAULTS, **raw}
    rules["first_screen"] = {**DEFAULTS["first_screen"],
                             **(raw.get("first_screen") or {})}
    rules["numbers"] = {**DEFAULTS["numbers"], **(raw.get("numbers") or {})}
    for rule in rules["keywords"]:
        rule["re"] = re.compile(rule["pattern"], re.IGNORECASE)
    return rules


def page_kind(page: str) -> str:
    """Вид страницы так, как его называют правила."""
    return CHANNEL_PAGE if page.startswith(CHANNEL_KINDS) else page


def _fits(pages, kind: str) -> bool:
    return ANY_PAGE in pages or kind in pages


def _money(entry: dict) -> str:
    """«было 49 000 → стало 54 000 ₽/мес (+10,2%)» из записи в файле дельты."""
    unit = f" {entry['единица']}" if entry.get("единица") else ""
    share = entry.get("изменение в процентах")
    percent = f" ({share:+.1f}%)".replace(".", ",") if share is not None else ""
    return (f"было {prices.pretty(entry['было'])} → "
            f"стало {prices.pretty(entry['стало'])}{unit}{percent}")


def _one_number(entry: dict) -> str:
    unit = f" {entry['единица']}" if entry.get("единица") else ""
    return f"{entry.get('как на странице', entry['значение'])}{unit}"


def _where(entry: dict) -> str:
    context = (entry.get("рядом") or "").strip()
    return f"  · {context[:80]}" if context else ""


def _same_numbers(fresh: list[dict], gone: list[dict]) -> tuple[list[dict], list[dict]]:
    """Убрать числа, которые «исчезли» и «появились» одновременно.

    Так выглядит не изменение, а сдвиг. Разбор цен Фазы 3 узнаёт вчерашнее
    число по трём строкам над ним; вставьте на страницу абзац — и у всех чисел
    ниже поменяется окружение, а значит, каждое из них честно отчитается, что
    исчезло в одном месте и появилось в другом. Проверено на снимке главной
    Речки: одна вставленная строка порождает пару «исчезло 20 % / появилось
    20 %», и без этой отсечки человек получал бы срочное сообщение «убрали
    тариф» каждый раз, когда конкурент дописал абзац.

    Одинаковое значение с одинаковой единицей на той же странице — то же самое
    число. Гасим парами: три исчезнувшие пятёрки против одной появившейся
    оставляют две исчезнувшие.
    """
    def mark(entry: dict) -> tuple:
        return (entry.get("значение"), entry.get("единица", ""))

    left = Counter(mark(n) for n in fresh) & Counter(mark(n) for n in gone)
    return _drop(fresh, Counter(left)), _drop(gone, Counter(left))


def _drop(numbers: list[dict], quota: Counter) -> list[dict]:
    out = []
    for entry in numbers:
        key = (entry.get("значение"), entry.get("единица", ""))
        if quota.get(key):
            quota[key] -= 1
            continue
        out.append(entry)
    return out


def _unique_changes(changed: list[dict]) -> list[dict]:
    """Одна цена — одна строка в сообщении.

    «49 000 ₽» на странице SalesAI написано дважды: в карточке тарифа отдельной
    строкой и внутри длинного описания «Free (40 минут/день навсегда), Team
    (49 000 ₽/мес — 10 AI-агентов…)». Правка цены меняет оба места, и без этой
    склейки человек получает «было 49 000 → стало 54 000» два раза подряд.

    Из повторов оставляем тот, где строка короче. Цена, стоящая в строке одна, —
    это ценник карточки, и над ним написано название тарифа; цена внутри абзаца
    — это упоминание, и рядом с ней оказываются пункты меню. Проверено на той же
    странице SalesAI: короткая строка дала окружение «Рекомендуем · Team»,
    длинная — «Войти · Переключить тему · Открыть меню».
    """
    best: dict[tuple, dict] = {}
    for entry in changed:
        key = (entry["было"], entry["стало"], entry.get("единица", ""))
        kept = best.get(key)
        if kept is None or len(entry.get("строка стала") or "") <                 len(kept.get("строка стала") or ""):
            best[key] = entry
    return list(best.values())


def _check_numbers(item: dict, rules: dict, verdict: Verdict) -> None:
    """Числа на коммерческой странице. Самый твёрдый сигнал системы."""
    numbers = item.get("числа")
    if not numbers:
        return

    changed = numbers.get("изменилось") or []
    fresh, gone = _same_numbers(numbers.get("появилось") or [],
                               numbers.get("исчезло") or [])
    if not (changed or fresh or gone):
        return

    show = int(rules["numbers"]["in_message"])
    flood = int(rules["numbers"]["flood"])

    # Пересборка прайса целиком — это не двести смен цены. Сказать «двести»
    # честнее, чем перечислить первые шесть и сделать вид, что это всё.
    if len(changed) + len(fresh) + len(gone) > flood:
        verdict.add("наводнение чисел",
                    f"разом изменилось {len(changed)}, появилось {len(fresh)} и "
                    f"исчезло {len(gone)} чисел — похоже на пересборку прайса "
                    f"целиком, смотреть глазами",
                    [f"₽ {_money(c)}{_where(c)}" for c in changed[:2]])
        return

    # Догадка «убрали тариф» уместна на странице тарифов и неуместна на главной:
    # там исчезнувшее число — это чаще всего обещание вроде «рост на 30%».
    tariffs = page_kind(item["страница"]) == "pricing"

    if changed:
        # Одна и та же цена часто написана на странице дважды — в описании
        # тарифа и в его карточке. Это одно изменение, а не два.
        unique = _unique_changes(changed)
        verdict.add("цена изменилась",
                    "изменилась цена" if len(unique) == 1
                    else f"изменилось цен: {len(unique)}",
                    [f"₽ {_money(c)}{_where(c)}" for c in unique[:show]])
    if gone:
        verdict.add("число исчезло",
                    "со страницы тарифов исчезло число — похоже, убрали тариф"
                    if tariffs else "со страницы исчезло число",
                    [f"₽ исчезло {_one_number(n)}{_where(n)}" for n in gone[:show]])
    if fresh:
        verdict.add("число появилось",
                    "на странице тарифов появилось число — похоже, добавили тариф"
                    if tariffs else "на странице появилось число",
                    [f"₽ появилось {_one_number(n)}{_where(n)}" for n in fresh[:show]])


def first_screen(lines: list[str], setup: dict) -> set[str]:
    """Строки первого экрана: первые содержательные, а не первые подряд.

    Выше заголовка на странице лежит меню сайта, и его пункты — короткие. Если
    считать строки подряд, у половины конкурентов первый экран целиком уйдёт на
    меню: замер по семнадцати главным страницам показал заголовок и на 2-й
    строке, и на 50-й. Поэтому строки короче min_chars пропускаются как меню и
    кнопки, а берутся первые lines строк с настоящим текстом.
    """
    depth = int(setup.get("lines", 6))
    least = int(setup.get("min_chars", 25))
    out = []
    for line in lines:
        if len(line) >= least:
            out.append(line)
            if len(out) >= depth:
                break
    return set(out)


def _check_first_screen(item: dict, rules: dict, verdict: Verdict,
                        old_lines: list[str] | None,
                        new_lines: list[str] | None) -> None:
    """Первый экран главной — главное обещание конкурента."""
    setup = rules["first_screen"]
    if not _fits(setup.get("pages", ["home"]), page_kind(item["страница"])):
        return

    delta = item["разница"]
    if new_lines is None and old_lines is None:
        # Снимок не найден — правило не отработало. Молчать об этом нельзя:
        # иначе «не проверили» выглядит как «проверили и ничего не нашли».
        verdict.notes.append("первый экран не проверен: снимков рядом не оказалось")
        return

    top_new = first_screen(new_lines or [], setup)
    top_old = first_screen(old_lines or [], setup)
    hits = [line for line in delta.get("добавлено", []) if line in top_new]
    hits += [line for line in delta.get("удалено", []) if line in top_old]
    if hits:
        verdict.add("первый экран", setup["say"],
                    [f"«{line[:QUOTE]}»" for line in hits[:3]])


def _hits(rule: dict, lines: list[str]) -> dict[str, str]:
    """Что именно нашлось на этой стороне: найденный кусок → строка целиком."""
    found: dict[str, str] = {}
    for line in lines:
        for match in rule["re"].finditer(line):
            key = " ".join(match.group(0).lower().split())
            found.setdefault(key, line)
    return found


def _check_keywords(item: dict, rules: dict, verdict: Verdict) -> None:
    """Слова рынка: гарантия, бесплатный тариф, коробка, лимит, смена тарифов.

    Главная тонкость здесь — переписанная строка. У SalesAI цена стоит внутри
    длинного описания: «Free (40 минут/день навсегда), Team (49 000 ₽/мес — 10
    AI-агентов, до 20 пользователей, 7000 минут) и Enterprise (On-Premise,
    безлимит)». Правка одной цены переписывает всю строку, и построчное
    сравнение честно кладёт её и в «удалено», и в «добавлено». Если смотреть на
    стороны по отдельности, в одном сообщении окажется «появился Enterprise» и
    «исчез Enterprise» разом — то есть очевидная чушь, после которой систему
    закрывают.

    Поэтому сравниваются не строки, а найденные в них куски: то, что есть по
    обе стороны, взаимно гасится. Осталось только с одной стороны — слово
    действительно появилось или действительно ушло. Осталось с обеих (было «до
    20 пользователей», стало «до 30») — это не появление и не исчезновение, а
    смена формулировки, и говорится ровно так.
    """
    kind = page_kind(item["страница"])
    delta = item["разница"]

    for rule in rules["keywords"]:
        if not _fits(rule.get("pages", [ANY_PAGE]), kind):
            continue
        allowed = rule.get("when", ["появилось"])
        added = _hits(rule, delta.get("добавлено", []))
        removed = _hits(rule, delta.get("удалено", []))

        fresh = {k: v for k, v in added.items() if k not in removed}
        gone = {k: v for k, v in removed.items() if k not in added}
        show_fresh = "появилось" in allowed and fresh
        show_gone = "исчезло" in allowed and gone

        if show_fresh and show_gone:
            verdict.add(rule["name"], f"{rule['name']}: формулировка изменилась",
                        [f"стало: «{line[:QUOTE]}»" for line in list(fresh.values())[:1]]
                        + [f"было: «{line[:QUOTE]}»" for line in list(gone.values())[:1]])
            continue
        for side, found in (("появилось", fresh), ("исчезло", gone)):
            if side not in allowed or not found:
                continue
            say = (rule.get("say") or {}).get(side) or rule["name"]
            verdict.add(rule["name"], say,
                        [f"«{line[:QUOTE]}»" for line in list(found.values())[:2]])


def judge(item: dict, rules: dict, old_lines: list[str] | None = None,
          new_lines: list[str] | None = None) -> Verdict:
    """Классифицировать одно изменение из diffs/<домен>/<страница>/<дата>.json.

    old_lines и new_lines — строки снимков, между которыми найдено изменение.
    Нужны только правилу первого экрана: без них оно не работает, и об этом
    честно пишется в приговор. Остальные правила читают саму дельту.
    """
    verdict = Verdict()
    _check_numbers(item, rules, verdict)
    _check_first_screen(item, rules, verdict, old_lines, new_lines)
    _check_keywords(item, rules, verdict)
    return verdict
