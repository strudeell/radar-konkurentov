"""Превращение страницы в снимок: видимый текст, очищенный от самоменяющегося.

Два решения технического плана живут здесь.

Решение №2 «сравниваем видимый текст, а не HTML». Сайты на Tilda, React и Next
переписывают разметку и имена стилей при каждой пересборке. Сравнение исходного
кода страницы давало бы «изменение!» каждый день на всех доменах.

Нормализация из того же решения: вырезать заведомо динамическое — счётчики,
«онлайн 47 человек», текущие даты, токены, номера сборки. Список того, что
вырезаем, лежит рядом в config/noise.yaml человеческим языком.

Отдельно про строки. Текст режется на строки по блокам страницы — заголовок,
абзац, пункт списка, ячейка. Это нужно следующей фазе: сравнение построчное,
и «добавился один заголовок» должно выглядеть как одна добавленная строка,
а не как «вся страница изменилась».
"""

import re
from pathlib import Path

from bs4 import BeautifulSoup, Comment

ROOT = Path(__file__).resolve().parent.parent

# Теги, внутри которых нет текста для человека.
_DROP_TAGS = ["script", "style", "noscript", "svg", "template", "iframe",
              "canvas", "object", "embed"]

# Строки короче этого выбрасываем: одиночные символы и обломки вёрстки
# («·», «/», «→») смысла не несут, а шуметь в диффах умеют.
_MIN_LINE = 2

_noise_cache: dict | None = None


def load_noise() -> dict:
    """Словарь шумодава из config/noise.yaml. Компилируется один раз за прогон."""
    global _noise_cache
    if _noise_cache is not None:
        return _noise_cache
    path = ROOT / "config" / "noise.yaml"
    if not path.exists():
        _noise_cache = {"global": [], "by_host": {}}
        return _noise_cache
    import yaml
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def compile_rules(rules):
        out = []
        for r in rules or []:
            out.append((
                r.get("name", ""),
                re.compile(r["pattern"], re.IGNORECASE | re.MULTILINE),
                r.get("replace", ""),
            ))
        return out

    _noise_cache = {
        "global": compile_rules(raw.get("global")),
        "by_host": {h: compile_rules(rules)
                    for h, rules in (raw.get("by_host") or {}).items()},
        "by_kind": {k: compile_rules(rules)
                    for k, rules in (raw.get("by_kind") or {}).items()},
    }
    return _noise_cache


def _rules_for(host: str, kind: str = "") -> list:
    """Правила для этой страницы: общие плюс те, что заданы её домену и виду."""
    noise = load_noise()
    rules = list(noise["global"])
    host = (host or "").lower()
    for suffix, extra in noise["by_host"].items():
        if host == suffix or host.endswith("." + suffix):
            rules.extend(extra)
    rules.extend(noise["by_kind"].get(kind, []))
    return rules


def strip_noise(text: str, host: str = "", kind: str = "") -> str:
    """Замена самоменяющихся кусков на постоянные метки."""
    for _name, pattern, replace in _rules_for(host, kind):
        text = pattern.sub(replace, text)
    return text


def visible_lines(html: str) -> list[str]:
    """Видимый текст постранично, по одной строке на блок разметки."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    lines = []
    for chunk in soup.get_text("\n").split("\n"):
        # \u00a0 — неразрывный пробел, его сайты ставят в цены и телефоны.
        chunk = re.sub(r"[\s\u00a0\u2009\u202f]+", " ", chunk).strip()
        if len(chunk) >= _MIN_LINE:
            lines.append(chunk)
    return lines


def plain_lines(text: str) -> list[str]:
    """То же для ответов, которые пришли не разметкой, а обычным текстом.

    Такой у нас один — salesai.ru/llms.txt, машиночитаемая выжимка оффера.
    Гнать её через разбор разметки нельзя: символы < и > в тексте
    воспринялись бы как теги и часть содержимого пропала бы.
    """
    out = []
    for chunk in text.split("\n"):
        chunk = re.sub(r"[\s\u00a0\u2009\u202f]+", " ", chunk).strip()
        if len(chunk) >= _MIN_LINE:
            out.append(chunk)
    return out


def to_snapshot(body: str, host: str = "", is_html: bool = True,
                kind: str = "", mask: bool = True) -> str:
    """Готовый текст снимка: то, что ляжет в файл и будет сравниваться завтра.

    Вид страницы (kind) влияет на очистку: на списке публикаций число отдельной
    строкой — счётчик просмотров и его надо убрать, на странице тарифов такое же
    число — цена и трогать её нельзя. Подробности в config/noise.yaml.

    Ключ mask отвечает на вопрос, ставить ли метки шумодава прямо в снимок.
    Сборщик сохраняет снимок **без** меток (mask=False), а метки ставит
    сравнение — diffing.mask. Причина найдена на живых данных: новость Mango
    Office «С 1 сентября 2026 года MANGO OFFICE обновляет тарификацию»
    превращалась в снимке в «С <дата> …», и человек получал сообщение о смене
    цен без даты, с которой они меняются. Метка нужна сравнению, а не читателю.

    Значение по умолчанию оставлено прежним ради тех, кто звал эту функцию
    раньше: замер шума и разбор разведки Фазы 0 сравнивают тексты между собой,
    и им всё равно, на каком шаге поставлена метка.
    """
    lines = visible_lines(body) if is_html else plain_lines(body)
    cleaned = []
    for line in lines:
        if mask:
            line = strip_noise(line, host, kind)
        line = re.sub(r"\s{2,}", " ", line).strip()
        if len(line) >= _MIN_LINE:
            cleaned.append(line)
    return "\n".join(cleaned)


def visible_text(html: str) -> str:
    """Один сплошной текст без переносов — для замера объёма и сверки страниц.

    Именно так считался baseline_visible_chars в Фазе 0, поэтому цифры
    сравнимы между фазами.
    """
    return " ".join(visible_lines(html))
