"""Общий модуль запросов для разведки Фазы 0.

Правила из технического плана (04-tehnicheskij-plan.md, Фаза 2 «Вежливость»):
User-Agent с контактом, пауза между запросами, таймаут, 2 повтора,
падение одного источника не роняет прогон.
"""

import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

def _load_config() -> dict:
    """Настройки человека — из config/config.yaml. Файл может отсутствовать."""
    cfg = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
    if not cfg.exists():
        return {}
    import yaml
    return yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}


_CFG = _load_config()
CONTACT = _CFG.get("contact_email", "TODO@okk-ai.ru")

# Контакт в User-Agent — условие вежливого обхода: владелец сайта должен иметь
# возможность написать нам, а не сразу банить. Заполняется в config/config.yaml.
UA_BOT = (
    f"{_CFG.get('bot_name', 'RadarBot')}/{_CFG.get('bot_version', '0.1')} "
    f"(+{_CFG.get('bot_url', 'https://okk-ai.ru')}; competitive monitoring; "
    f"contact: {CONTACT})"
)

if CONTACT.startswith("TODO"):
    import warnings
    warnings.warn(
        "В config/config.yaml не заполнен contact_email — в User-Agent уходит заглушка. "
        "Для разведки это терпимо, для регулярного сбора нет.",
        stacklevel=2,
    )
UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

TIMEOUT = 20.0
RETRIES = 2
DELAY_SAME_HOST = 2.0

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "_raw"
RAW.mkdir(parents=True, exist_ok=True)

_last_hit: dict[str, float] = {}

# Пауза больше двух секунд для отдельных сайтов. Сюда сборщик кладёт значение,
# если сайт сам попросил его в robots.txt (директива Crawl-delay). В Фазе 0 такой
# просьбы не выставил ни один из 19 доменов, но правило может появиться завтра.
HOST_DELAY: dict[str, float] = {}


def _polite_wait(url: str) -> None:
    host = urlparse(url).netloc
    delay = max(DELAY_SAME_HOST, HOST_DELAY.get(host, 0.0))
    prev = _last_hit.get(host)
    if prev is not None:
        gap = time.monotonic() - prev
        if gap < delay:
            time.sleep(delay - gap)
    _last_hit[host] = time.monotonic()


def fetch(url: str, ua: str = UA_BOT, timeout: float | None = None,
          retries: int | None = None) -> dict:
    """Один URL. Никогда не бросает исключение — возвращает результат со статусом."""
    timeout = TIMEOUT if timeout is None else timeout
    retries = RETRIES if retries is None else retries
    _polite_wait(url)
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }
    last_err = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(
                follow_redirects=True, timeout=timeout, headers=headers, verify=True
            ) as c:
                r = c.get(url)
            return {
                "url": url,
                "final_url": str(r.url),
                "status": r.status_code,
                "redirected": str(r.url).rstrip("/") != url.rstrip("/"),
                "content_type": r.headers.get("content-type", ""),
                "bytes": len(r.content),
                "text": r.text,
                "error": None,
                "ua": "bot" if ua == UA_BOT else "browser",
            }
        except Exception as e:  # сеть, TLS, таймаут — всё сюда
            last_err = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    return {
        "url": url,
        "final_url": None,
        "status": None,
        "redirected": False,
        "content_type": "",
        "bytes": 0,
        "text": "",
        "error": last_err,
        "ua": "bot" if ua == UA_BOT else "browser",
    }


def fetch_with_fallback(url: str) -> dict:
    """Честный бот-UA. Если не 200 — повтор браузерным UA.

    Разделяет два разных случая: сайт режет по User-Agent (лечится заголовком)
    и сайт держит настоящую защиту (нужна браузерная автоматизация).
    """
    res = fetch(url, UA_BOT)
    if res["status"] == 200:
        res["ua_fallback_needed"] = False
        return res
    alt = fetch(url, UA_BROWSER)
    alt["bot_ua_status"] = res["status"]
    alt["bot_ua_error"] = res["error"]
    alt["ua_fallback_needed"] = alt["status"] == 200
    return alt


def fetch_probe(url: str) -> dict:
    """Быстрая проверка кандидата-пути: короткий таймаут, без повторов.

    Браузерный UA пробуем только на кодах, которые означают «нас отшили»
    (401/403/429). На 404 смена UA бессмысленна — страницы просто нет,
    и повтор лишь удваивает нагрузку на чужой сайт.
    """
    res = fetch(url, UA_BOT, timeout=8.0, retries=0)
    res["ua_fallback_needed"] = False
    if res["status"] in (401, 403, 429):
        alt = fetch(url, UA_BROWSER, timeout=12.0, retries=1)
        alt["bot_ua_status"] = res["status"]
        alt["ua_fallback_needed"] = alt["status"] == 200
        return alt
    return res


def visible_text(html: str) -> str:
    """Видимый текст, а не HTML — решение №2 технического плана."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    return re.sub(r"\s+", " ", text).strip()


def links(html: str, base_url: str) -> list[tuple[str, str]]:
    """(абсолютный href, текст ссылки) со страницы."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        out.append((urljoin(base_url, href), re.sub(r"\s+", " ", a.get_text()).strip()))
    return out


def save_raw(name: str, html: str) -> None:
    (RAW / f"{name}.html").write_text(html, encoding="utf-8")


def dump_json(name: str, data) -> None:
    (ROOT / "data" / f"{name}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
