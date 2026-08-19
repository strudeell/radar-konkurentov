"""Поиск разницы между двумя снимками — сердце Фазы 3.

Сборщик Фазы 2 отвечает только «текст тот же» или «текст другой». Здесь текст
разбирается построчно: что добавилось, что исчезло, что просто переехало на
другое место и сколько символов на самом деле затронуто.

Три решения, без которых детектор врёт.

**Переезд строки — не изменение.** На странице блога новая статья выталкивает
остальные вниз, на главной ротация отзывов меняет их порядок. Построчное
сравнение честно показывает такие строки и удалёнными, и добавленными сразу.
Если их не вычесть, объём «изменения» раздувается в разы, и порог, за которым
изменение попадает в дайджест, начинает срабатывать от перестановки блоков.
Поэтому строки, которые ушли в одном месте и дословно появились в другом,
считаются переехавшими и в объём изменения не входят.

**Метки шумодава сами по себе — не изменение.** Нормализация Фазы 2 заменяет
дату на «<дата>», счётчик на «<счётчик>». Если строка целиком состоит из таких
меток, её появление или исчезновение не несёт смысла: это сдвинулась разметка,
а не поменялся текст.

**Объём считаем по видимому тексту, а не по числу строк.** «Изменилось четыре
строки» не говорит ничего: это может быть пункт меню и абзац на тысячу знаков.
Порог в плане задан в символах, и мерить надо в них же.

Про autojunk. Стандартный SequenceMatcher при длине больше 200 элементов сам
объявляет часто встречающиеся строки «мусором» и перестаёт по ним
синхронизироваться. Наши страницы длиннее: у Roistat 1682 строки, из них
уникальных 821 — половина повторов. Это ровно тот случай, на который эвристика
рассчитана, и предсказать, как она поделит строки завтра, нельзя. Поэтому она
выключена явно.

Честности ради: на сегодняшних снимках разницы между включённой и выключенной
эвристикой не нашлось — проверено на паре Mango Office и на прайс-листе Roistat,
оба раза результат совпал до символа. Выключено как страховка от поведения,
которым мы не управляем, а не как лечение уже случившейся поломки.
"""

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Метки, которые ставит шумодав Фазы 2 вместо самоменяющихся кусков.
_MARKS = r"<дата>|<время>|<когда>|<счётчик>|<идентификатор>|©\s*<год>"

# Строка целиком из меток и знаков препинания между ними.
_ONLY_MARKS = re.compile(rf"^\s*(?:(?:{_MARKS})[\s·•|,.:;–—/-]*)+$", re.IGNORECASE)

# Строка без единой буквы и цифры: обломки вёрстки вроде «·», «→», «— —».
_NO_CONTENT = re.compile(r"^[^\w]*$", re.UNICODE)

# Блоки крупнее этого не выравниваем построчно: сопоставление «каждая с каждой»
# на паре по триста строк — сотня тысяч сравнений ради красивой печати.
_MAX_ALIGN = 60

_ignore_cache: list | None = None


def ignore_rules() -> list:
    """Правила «эту строку в изменениях не считать» из noise.yaml, раздел diff."""
    global _ignore_cache
    if _ignore_cache is not None:
        return _ignore_cache
    path = ROOT / "noise.yaml"
    rules = []
    if path.exists():
        import yaml
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for rule in (raw.get("diff") or {}).get("ignore_lines") or []:
            rules.append((rule.get("name", ""),
                          re.compile(rule["pattern"], re.IGNORECASE)))
    _ignore_cache = rules
    return rules


def split_lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if line.strip()]


def is_noise(line: str) -> bool:
    """Строка, появление или исчезновение которой ничего не значит."""
    if _ONLY_MARKS.match(line) or _NO_CONTENT.match(line):
        return True
    return any(pattern.search(line) for _name, pattern in ignore_rules())


@dataclass
class Pair:
    """Строка, которую переписали: было → стало. Для читаемого вывода."""
    before: str
    after: str
    context: str


@dataclass
class Delta:
    """Разница между двумя снимками одной страницы."""
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    pairs: list[Pair] = field(default_factory=list)
    lines_before: int = 0
    lines_after: int = 0

    @property
    def added_chars(self) -> int:
        return sum(len(line) for line in self.added)

    @property
    def removed_chars(self) -> int:
        return sum(len(line) for line in self.removed)

    @property
    def changed_chars(self) -> int:
        """Сколько символов видимого текста затронуто. По нему бьёт порог."""
        return self.added_chars + self.removed_chars

    @property
    def empty(self) -> bool:
        return not self.added and not self.removed

    def to_dict(self, sample: int = 0) -> dict:
        """Дельта для записи в файл. sample — сколько строк оставить, 0 — все."""
        cut = (lambda seq: seq[:sample]) if sample else (lambda seq: seq)
        return {
            "добавлено строк": len(self.added),
            "удалено строк": len(self.removed),
            "переехало строк": len(self.moved),
            "отсеяно шумодавом строк": len(self.ignored),
            "добавлено символов": self.added_chars,
            "удалено символов": self.removed_chars,
            "затронуто символов": self.changed_chars,
            "строк в снимке было": self.lines_before,
            "строк в снимке стало": self.lines_after,
            "добавлено": cut(self.added),
            "удалено": cut(self.removed),
            "переехало": cut(self.moved),
            "отсеяно шумодавом": cut(self.ignored),
            "переписано": [{"было": p.before, "стало": p.after, "рядом": p.context}
                           for p in cut(self.pairs)],
        }


def _context_before(lines: list[str], index: int) -> str:
    """Ближайшая осмысленная строка выше — чтобы понимать, где это на странице."""
    for line in reversed(lines[max(0, index - 4):index]):
        if len(line) >= 3 and not is_noise(line):
            return line[:120]
    return ""


def _align(before: list[str], after: list[str], context: str) -> list[Pair]:
    """Сопоставить переписанные строки друг с другом внутри одного места.

    Нужно только для читаемого «было → стало». Числа со страниц тарифов ищет
    отдельный разбор в prices.py, и он на это сопоставление не опирается.
    """
    if not before or not after or max(len(before), len(after)) > _MAX_ALIGN:
        return []
    pairs = []
    free = list(range(len(after)))
    for old in before:
        best, best_ratio = None, 0.0
        for j in free:
            ratio = difflib.SequenceMatcher(None, old, after[j]).ratio()
            if ratio > best_ratio:
                best, best_ratio = j, ratio
        # Ниже половины совпадения это уже не «переписали строку», а разные строки.
        if best is not None and best_ratio >= 0.5:
            pairs.append(Pair(before=old, after=after[best], context=context))
            free.remove(best)
    return pairs


def _subtract(lines: list[str], taken: Counter) -> list[str]:
    """Убрать из списка ровно столько повторов каждой строки, сколько переехало."""
    left = Counter(taken)
    out = []
    for line in lines:
        if left.get(line):
            left[line] -= 1
            continue
        out.append(line)
    return out


def compare(old_text: str, new_text: str) -> Delta:
    """Разница между вчерашним и сегодняшним снимком одной страницы."""
    old = split_lines(old_text)
    new = split_lines(new_text)
    delta = Delta(lines_before=len(old), lines_after=len(new))

    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    added: list[str] = []
    removed: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed.extend(old[i1:i2])
        added.extend(new[j1:j2])
        if tag == "replace":
            delta.pairs.extend(_align(old[i1:i2], new[j1:j2],
                                      _context_before(old, i1)))

    # Шаг первый: убрать строки, которые ничего не значат сами по себе.
    delta.ignored = [line for line in added + removed if is_noise(line)]
    added = [line for line in added if not is_noise(line)]
    removed = [line for line in removed if not is_noise(line)]

    # Шаг второй: вычесть переезды. Строка, ушедшая в одном месте и дословно
    # появившаяся в другом, — это перестановка блоков, а не новый текст.
    moved = Counter(added) & Counter(removed)
    delta.moved = sorted(moved.elements())
    delta.added = _subtract(added, moved)
    delta.removed = _subtract(removed, moved)

    # Пары «было → стало» переживают вычитание, только если уцелели обе половины.
    survived = set(delta.added)
    was = set(delta.removed)
    delta.pairs = [p for p in delta.pairs if p.after in survived and p.before in was]
    return delta
