#!/usr/bin/env python3
"""Дочитать новость: сходить по ссылке и принести то, ради чего её открывают.

Зачем это понадобилось. Радар видит ленту новостей конкурента, а не сами
новости. В ленте стоит заголовок и одна строка описания — «Актуализация
тарифов. С 1 сентября 2026 года MANGO OFFICE обновляет тарификацию ИИ-функций
Речевой аналитики». Этого хватает, чтобы понять, что дело срочное, и не хватает
ни для чего больше: **на что** меняются цены, написано внутри новости.

Владелец, получив такое сообщение, всё равно открывает ссылку и читает сам.
Значит, эту работу надо делать за него — тем более что она механическая: найти
на странице ссылку с тем же заголовком, открыть, взять начало текста.

Границы, которые здесь важны.

**Ходим только за срочным.** Дочитывание — это лишние запросы к чужому сайту, и
делать их ради каждой статьи в блоге незачем: обычное всё равно ждёт
понедельника, и человек прочитает его по ссылке сам. Ходим только за тем, о чём
собираемся написать немедленно, и не больше двух новостей за прогон.

**Робот остаётся вежливым.** Тот же User-Agent с контактным адресом, та же
пауза между запросами, та же проверка robots.txt, что и у сборщика Фазы 2.
Правило простое: раз мы не нарушаем правила чужого сайта при ежедневном обходе,
то и здесь не нарушаем.

**Не дочитали — не беда.** Ссылка не нашлась, сайт не ответил, robots.txt не
разрешил — сообщение уходит без вставки. Срочное сообщение, не отправленное
из-за того, что не открылась дополнительная страница, — куда худший исход, чем
сообщение без подробностей.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import normalize  # noqa: E402
import probe  # noqa: E402

# Отсюда начинается не новость, а обвязка сайта: похожие статьи, кейсы,
# подписка. Список короткий и заведомо неполный — его дописывает обкатка.
STOP = re.compile(
    r"^(истории наших клиентов|актуальное|читайте также|похожие|ещё по теме|"
    r"смотрите также|подпишитесь|подписаться|рекомендуем|популярное|"
    r"другие новости|все новости|поделиться|получите консультацию|"
    r"оставьте заявку|заполните форму|нажимая кнопку)", re.IGNORECASE)

# «С 1 сентября 2026 года…» — самое ценное в новости о смене тарифов: не что
# меняется, а когда. Ради этой строки всё и затевалось.
SINCE = re.compile(
    r"\bс\s+\d{1,2}\s+(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|"
    r"октябр|ноябр|декабр)\S*\s+\d{4}(\s*год\S*)?", re.IGNORECASE)

# Строка, в которой есть деньги или объём: «от 0,3 ₽ за 1 000 символов».
MONEY = re.compile(r"\d[\d  ]*[.,]?\d*\s*(₽|руб|%|символ|минут|звонк|мин\b)",
                   re.IGNORECASE)


def _same(one: str, other: str) -> bool:
    """Заголовок в ленте и текст ссылки — это одно и то же?"""
    clean = (lambda s: " ".join(s.lower().split()).strip(" .!?—–-"))
    one, other = clean(one), clean(other)
    return bool(one) and (one == other or (len(one) > 25 and one in other))


def find_link(html: str, base_url: str, headline: str) -> str | None:
    """Ссылка на новость с таким заголовком. Первая подходящая."""
    for url, text in probe.links(html, base_url):
        if _same(headline, text):
            return url
    return None


def _from(lines: list[str], start: int, headline: str, limit: int) -> list[str]:
    """Текст, идущий за строкой start, до конца полезного."""
    out = []
    for line in lines[start + 1:]:
        if STOP.match(line.strip()):
            break
        if _same(headline, line):
            continue          # заголовок повторён — это ещё не текст
        # Дата публикации и счётчик просмотров стоят между заголовком и текстом.
        # Они уже есть в сообщении и сами по себе ничего не говорят.
        if len(line) < 12 and not MONEY.search(line):
            continue
        out.append(line)
        if len(out) >= limit:
            break
    return out


def _body(lines: list[str], headline: str, limit: int) -> list[str]:
    """Начало новости: строки после заголовка и до конца полезного текста.

    Заголовок встречается на странице не два раза, а больше: в хлебных крошках,
    над текстом и ещё раз в блоке «Актуальное» сбоку — там у конкурента лежит
    другая новость с точно таким же названием «Актуализация тарифов». Проверено
    на живой странице Mango Office: если брать последнее вхождение, в сообщение
    попадает соседняя новость про номера 8-800 вместо той, ради которой пришли.

    Поэтому пробуются все вхождения по очереди и берётся первое, за которым
    идёт текст с датой вступления в силу. Нет такой даты ни у одного — берётся
    первое, за которым вообще есть текст: тело новости всегда стоит выше
    боковых блоков.
    """
    starts = [i for i, line in enumerate(lines) if _same(headline, line)]
    first = []
    for start in starts:
        body = _from(lines, start, headline, limit)
        if not body:
            continue
        if any(SINCE.search(line) for line in body):
            return body
        first = first or body
    return first


def read(source_url: str, headlines, limit: int = 6, robots=None) -> dict | None:
    """Открыть новость по заголовку из ленты и принести её начало.

    Заголовков даётся несколько — все строки, которые в ленте появились. Какая
    из них заголовок, а какая описание под ним, снаружи не знает никто: пробуем
    по очереди и берём первую, для которой на странице нашлась ссылка.

    Лента при этом скачивается один раз на все попытки. Возвращается None, если
    дочитать не вышло: пусть лучше сообщение уйдёт без подробностей, чем не
    уйдёт вовсе.
    """
    if isinstance(headlines, str):
        headlines = [headlines]

    listing = probe.fetch(source_url)
    if listing.get("error") or not listing.get("text"):
        return None

    base = listing.get("final_url") or source_url
    url, headline = None, ""
    for candidate in headlines:
        url = find_link(listing["text"], base, candidate)
        if url:
            headline = candidate
            break
    if not url:
        return None

    if robots is not None:
        allowed, why = robots.allowed(url)
        if not allowed:
            return {"адрес": url, "строки": [], "не прочитано": f"robots.txt: {why}"}

    article = probe.fetch(url)
    if article.get("error") or not article.get("text"):
        return {"адрес": url, "строки": [],
                "не прочитано": article.get("error") or "пустой ответ"}

    lines = normalize.visible_lines(article["text"])
    body = _body(lines, headline, limit)
    if not body:
        return {"адрес": url, "строки": [], "не прочитано": "текст новости не нашёлся"}

    since = ""
    for line in body:
        found = SINCE.search(line)
        if found:
            since = found.group(0)
            break

    return {
        "адрес": url,
        "заголовок": headline,
        "с какого числа": since,
        "строки": body,
        "цены": [line for line in body if MONEY.search(line)],
    }
