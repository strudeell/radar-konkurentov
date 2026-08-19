"""Отдельное извлечение чисел со страниц тарифов.

Зачем отдельно, а не общим сравнением текста. Общий разбор скажет «на странице
тарифов изменилась одна строка» — этого мало. Нужно «было 21 900 ₽ → стало
24 900 ₽»: в плане это названо самым ценным сигналом во всей системе, и он
слишком мал, чтобы поймать его объёмом. Смена цены задевает восемь символов при
пороге в сто двадцать — общий порог убил бы главный сигнал, если бы числа не
разбирались своим ходом.

Что здесь делается и чего сознательно не делается.

**Делается:** со страницы снимаются все осмысленные числа вместе с окружением —
единицей измерения, строкой, в которой число стоит, и ближайшими заголовками
выше. По этому окружению вчерашнее число находит сегодняшнее, и разница выдаётся
как «было → стало».

**Не делается: разбор тарифа как таблицы** — «тариф Team, цена 49 000, включено
7000 минут, сверх лимита 5 ₽». Проверено на живых снимках: у SalesAI цена стоит
строкой сразу после названия тарифа, у Imot.io между названием и ценой лежит
число включённых минут, а подпись «₽/мес.» относится не к числу перед ней, а к
числу после. Это семнадцать разных страниц с семнадцатью разными раскладками —
семнадцать отдельных разборов, которые сломаются при первой же пересборке
чужого сайта. Отсюда же не считается эффективная цена минуты (цена ÷ включённые
минуты): она требует уверенного знания, какое из чисел цена, а какое минуты.
Человек и ассистент получают «было → стало» с окружением и видят это сами.

Что из этого следует. Радар отвечает на вопрос «какое число на странице тарифов
изменилось и что написано вокруг него», а не «как теперь устроена тарифная
сетка конкурента». Это меньше, чем хотелось бы, зато работает на всех
семнадцати страницах одинаково и не врёт.
"""

import re
from collections import Counter
from dataclasses import dataclass

# Число с пробельными разделителями тысяч: 150 000, 1 000 000, 49 000, 0,0, 7000.
# Первая половина шаблона обязательно требует групп по три цифры — иначе два
# числа, стоящие рядом через пробел, склеились бы в одно.
_NUMBER = re.compile(r"\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")

# Единица сразу за числом. Валюта, время, люди, проценты.
_UNIT = re.compile(
    r"^\s*(₽|руб\.?|рублей|рубля|р\.|%|мин\.?|минут\w*|сек\w*|час\w*|"
    r"чел\.?|человек\w*|пользовател\w*|сотрудник\w*|менеджер\w*|"
    r"дн\w*|дней|день|сут\w*|мес\.?|месяц\w*|год\w*|лет|"
    r"звонк\w*|обращен\w*|диалог\w*|разговор\w*|шт\.?|\$|€)",
    re.IGNORECASE)

# Множитель перед единицей: «100 тыс руб», «5 млн ₽».
_SCALE = re.compile(r"^\s*(тыс\.?|тысяч\w*|млн\.?|миллион\w*|млрд\.?|миллиард\w*)\s*",
                    re.IGNORECASE)

# Приписка к единице: «/мес», «в месяц», «/год». Уточняет, но не обязательна.
_PER = re.compile(r"^\s*[/·]?\s*(в\s+)?(мес\w*|месяц|год|году|день|дн\w*|"
                  r"минуту|звонок|пользовател\w*|сотрудника)\b", re.IGNORECASE)

# Строка, в которой стоит только единица измерения: «₽/мес.», «в месяц».
_UNIT_ONLY = re.compile(r"^\s*[/·]?\s*(₽|руб\.?|\$|€)?\s*[/·]?\s*"
                        r"((в\s+)?(мес\w*|год\w*|день|дн\w*|минуту))?\s*\.?\s*$",
                        re.IGNORECASE)

# Телефон в строке. Числа из таких строк не берём совсем.
_PHONE = re.compile(r"\+7|\b8\s*\(|\d{3}[-\s]\d{2}[-\s]\d{2}\b")

# Слова, после которых число — не цена и не лимит: номер закона, стандарта, версии.
_NOT_A_PRICE = re.compile(r"(№|ФЗ|ISO|ГОСТ|СТО|версия|версии|v\.?|п\.|ст\.|"
                          r"индекс|инн|огрн|кпп|бик|счёт|счет)\s*$", re.IGNORECASE)

# Валюта, стоящая перед числом: «от ₽49 000».
_CURRENCY_BEFORE = re.compile(r"(₽|\$|€|руб\.?)\s*$", re.IGNORECASE)

_YEARS = range(1990, 2036)


@dataclass
class Number:
    """Число со страницы вместе со всем, что позволяет узнать его завтра."""
    value: float
    raw: str
    unit: str
    line: str
    template: str
    context: str
    index: int          # какое это по счёту число с таким же ключом на странице
    line_no: int = -1   # место строки на странице; строки повторяются, номера нет

    @property
    def key(self) -> tuple:
        return (self.context, self.template, self.index)

    def human(self) -> str:
        return f"{self.raw} {self.unit}".strip()

    def to_dict(self) -> dict:
        out = {"значение": self.value, "как на странице": self.raw,
               "строка": self.line, "рядом": self.context}
        if self.unit:
            out["единица"] = self.unit
        return out


def _parse(raw: str) -> float:
    plain = raw.replace(" ", "").replace(" ", "").replace(" ", "")
    plain = plain.replace(",", ".")
    return float(plain)


def _pretty(value: float) -> str:
    if value == int(value):
        return f"{int(value):,}".replace(",", " ")
    return f"{value:g}"


def pretty(value: float) -> str:
    """То же самое, но для соседних модулей: числа в сообщениях Фазы 4.

    В файле дельты цена лежит числом (49000.0), а человеку в Telegram нужно
    «49 000». Форматирование должно быть одно и то же в отчёте детектора и в
    сообщении бота, иначе одна и та же цена в двух местах выглядит по-разному.
    """
    return _pretty(value)


def _unit_at(text: str) -> str:
    """Единица измерения, начинающаяся в этом месте строки, вместе с припиской.

    Множитель («100 тыс руб») остаётся частью подписи, а не умножается на число:
    в отчёте должно стоять то, что написано на странице. «Было 100 → стало 150
    тыс руб» человек прочитает правильно, а «было 100 000 → стало 150 000» уже
    домысливание за конкурента.
    """
    scale = _SCALE.match(text)
    if scale:
        text = text[scale.end():]
    found = _UNIT.match(text)
    if not found:
        return scale.group(1).strip().rstrip(".").lower() if scale else ""
    unit = found.group(1).strip().rstrip(".")
    if scale:
        unit = f"{scale.group(1).strip().rstrip('.')} {unit}"
    tail = _PER.match(text[found.end():])
    if tail:
        unit = f"{unit}/{tail.group(2).strip().rstrip('.')}"
    return unit.lower()


def _mask(line: str) -> str:
    """Строка без чисел: «Предоплата за 3 месяца · 147 000 ₽» → «... <N> ... <N> ₽».

    По ней вчерашнее число узнаёт сегодняшнее. Маскируются все числа сразу:
    иначе изменение соседнего числа в той же строке рвало бы связь.
    """
    return _NUMBER.sub("<N>", line)[:200]


def _context_for(lines: list[str], index: int, depth: int = 3) -> str:
    """До трёх ближайших строк выше без единой цифры — обычно это название тарифа.

    Одной строки мало, и это видно на живых данных: у Imot.io пять тарифных
    блоков подряд, и над каждым числом стоит одинаковое «Количество включённых
    минут анализа». По одной строке все пять чисел неотличимы, по трём —
    в контекст попадает название тарифа, и они расходятся.

    Строки из одной единицы измерения («₽/мес.») в контекст не берутся: они
    стоят у каждого тарифа и своего не говорят, зато вытесняют из окна ровно ту
    строку, ради которой окно и заведено, — название тарифа.
    """
    out = []
    for line in reversed(lines[max(0, index - 12):index]):
        if any(ch.isdigit() for ch in line) or len(line) < 3:
            continue
        if _UNIT_ONLY.match(line.strip()):
            continue
        out.append(line[:60])
        if len(out) == depth:
            break
    return " · ".join(reversed(out))[:180]


def extract(lines: list[str]) -> list[Number]:
    """Все осмысленные числа страницы с окружением, в порядке появления."""
    found: list[Number] = []
    seen: dict[tuple, int] = {}

    for position, line in enumerate(lines):
        if _PHONE.search(line):
            continue
        template = _mask(line)
        context = _context_for(lines, position)

        for match in _NUMBER.finditer(line):
            raw = match.group(0)
            digits = sum(ch.isdigit() for ch in raw)
            before = line[:match.start()]
            after = line[match.end():]

            # Реквизиты: ИНН десять цифр, ОГРН тринадцать. Ценой не бывают.
            if digits >= 9:
                continue
            if _NOT_A_PRICE.search(before.strip()):
                continue

            unit = _unit_at(after)
            if not unit and _CURRENCY_BEFORE.search(before):
                unit = _CURRENCY_BEFORE.search(before).group(1).lower().rstrip(".")

            value = _parse(raw)
            if not unit:
                # Число без единицы разбираем осторожно. Год — не цена, а «7» в
                # «7 фактов» и «3 шага» тоже: мелочь без подписи шумит, а стоит
                # ноль. Крупное число без подписи оставляем — на страницах
                # тарифов это ровно цена, проверено в Фазе 2 на 416 строках.
                if int(value) in _YEARS and value == int(value):
                    continue
                if value < 10:
                    continue

            key = (context, template, 0)
            index = seen.get(key[:2], 0)
            seen[key[:2]] = index + 1
            found.append(Number(value=value, raw=raw.strip(), unit=unit, line=line,
                                template=template, context=context, index=index,
                                line_no=position))

    _attach_unit_lines(lines, found)
    return found


def _attach_unit_lines(lines: list[str], found: list[Number]) -> None:
    """Приписать числу единицу, стоящую отдельной строкой.

    На живых страницах встречаются оба порядка, и это выяснилось только при
    разборе снимков. У SalesAI: «49 000 ₽», следом строка «/мес» — приписка к
    числу выше, у которого валюта уже есть. У Imot.io: «150 000», строка
    «₽/мес.», строка «399 990» — подпись относится к числу ниже, а над ней
    стоит количество минут. Отсюда правило: если у числа выше валюта уже есть,
    строка-единица уточняет его; если нет — она принадлежит числу ниже.

    Соседи ищутся по номеру строки, а не по её тексту. Разница видна на
    прайс-листе Roistat: строка «7 4212, Хабаровск, 25 ₽ /» встречается на
    странице дважды, и пока соседство считалось по тексту, одно и то же число
    получало приписку два раза — «25 ₽/день/день».
    """
    by_line: dict[int, list[Number]] = {}
    for number in found:
        by_line.setdefault(number.line_no, []).append(number)

    for position, line in enumerate(lines):
        text = line.strip()
        if not text or not _UNIT_ONLY.match(text) or not any(
                ch.isalpha() or ch in "₽$€" for ch in text):
            continue
        unit = text.strip("/· .").lower()

        above = by_line.get(position - 1) if position else None
        below = by_line.get(position + 1) if position + 1 < len(lines) else None

        if above and above[-1].unit:
            merged = f"{above[-1].unit}{'' if unit.startswith('/') else '/'}{unit}"
            above[-1].unit = merged.replace("//", "/").rstrip("/.")
        elif above and not below:
            above[-1].unit = unit
        elif below and not below[0].unit:
            below[0].unit = unit


@dataclass
class Change:
    """Изменение одного числа: было столько, стало столько."""
    context: str
    line_before: str
    line_after: str
    was: float
    now: float
    unit: str

    @property
    def percent(self) -> float | None:
        if not self.was:
            return None
        return round((self.now - self.was) / self.was * 100, 1)

    def human(self) -> str:
        arrow = f"{_pretty(self.was)} → {_pretty(self.now)}"
        unit = f" {self.unit}" if self.unit else ""
        share = f" ({self.percent:+.1f}%)" if self.percent is not None else ""
        where = f"  [{self.context}]" if self.context else ""
        return f"было {arrow}{unit}{share}{where}"

    def to_dict(self) -> dict:
        return {"было": self.was, "стало": self.now, "единица": self.unit,
                "изменение в процентах": self.percent, "рядом": self.context,
                "строка была": self.line_before, "строка стала": self.line_after}


def _groups(numbers: list[Number]) -> dict:
    out: dict[tuple, list[Number]] = {}
    for number in numbers:
        out.setdefault((number.context, number.template), []).append(number)
    return out


def _change(stale: Number, fresh: Number) -> Change:
    return Change(context=fresh.context or stale.context, line_before=stale.line,
                  line_after=fresh.line, was=stale.value, now=fresh.value,
                  unit=fresh.unit or stale.unit)


def compare(old_lines: list[str], new_lines: list[str]) -> dict:
    """Сравнить числа двух снимков одной страницы.

    Сопоставление идёт по окружению, а не по месту на странице: строка,
    ставшая триста первой вместо трёхсотой, изменением не является. Числа с
    одинаковым окружением собираются в группу — обычно это один тарифный блок.

    Дальше важная тонкость, найденная на снимке Imot.io. Пока в группе столько
    же чисел, сколько было вчера, они сопоставляются по порядку — это обычный
    случай, и правка цены видна сразу. Но если в группе стало на одно число
    меньше (у Imot.io тарифные блоки повторяются для каждого срока оплаты, и
    удаление одного блока сдвигает всю очередь), сопоставление по порядку
    выдаёт цепочку выдуманных изменений: 53 990 → 50 750 → 47 499 → 40 499.
    Ни одно из них не происходило, сместился номер. Поэтому при разном размере
    группы совпавшие значения сначала вычитаются как несменившиеся, и лишь
    остаток идёт в «появилось» и «исчезло». Пара «одно ушло, одно пришло»
    считается изменением цены — это ровно тот случай, ради которого всё
    затевалось.
    """
    was = extract(old_lines)
    now = extract(new_lines)
    old_groups = _groups(was)
    new_groups = _groups(now)

    changed: list[Change] = []
    gone: list[Number] = []
    fresh_only: list[Number] = []

    for key in list(old_groups) + [k for k in new_groups if k not in old_groups]:
        before = old_groups.get(key, [])
        after = new_groups.get(key, [])

        if len(before) == len(after):
            for stale, fresh in zip(before, after):
                if stale.value != fresh.value:
                    changed.append(_change(stale, fresh))
            continue

        common = Counter(n.value for n in before) & Counter(n.value for n in after)
        left_old = _drop_matched(before, common)
        left_new = _drop_matched(after, common)
        if len(left_old) == 1 and len(left_new) == 1:
            changed.append(_change(left_old[0], left_new[0]))
        else:
            gone.extend(left_old)
            fresh_only.extend(left_new)

    return {
        "изменилось": changed,
        "появилось": fresh_only,
        "исчезло": gone,
        "всего чисел было": len(was),
        "всего чисел стало": len(now),
    }


def _drop_matched(numbers: list[Number], common: Counter) -> list[Number]:
    """Убрать из группы числа, значение которых есть и вчера, и сегодня."""
    left = Counter(common)
    out = []
    for number in numbers:
        if left.get(number.value):
            left[number.value] -= 1
            continue
        out.append(number)
    return out


def summary(result: dict) -> dict:
    """Результат сравнения в вид, который кладётся в файл дельты."""
    return {
        "изменилось": [c.to_dict() for c in result["изменилось"]],
        "появилось": [n.to_dict() for n in result["появилось"]],
        "исчезло": [n.to_dict() for n in result["исчезло"]],
        "всего чисел было": result["всего чисел было"],
        "всего чисел стало": result["всего чисел стало"],
    }
