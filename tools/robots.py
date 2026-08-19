"""Соблюдение robots.txt — правил, которые сайт выставляет роботам.

Из технического плана, Фаза 2: «robots.txt уважаем». В Фазе 0 файлы всех
19 доменов были прочитаны глазами и запретов, которые нам мешают, не нашлось.
Но прочитать один раз и соблюдать каждый день — разные вещи: сайт может закрыть
раздел завтра, и радар обязан это заметить сам, а не через письмо от владельца.

Файл читается один раз за прогон на каждый домен и держится в памяти.

Отдельно про случай «файл не отдался». Стандарт различает два ответа:
    нет файла (404) — ограничений нет, ходить можно;
    сервер сломался (5xx) — ходить нельзя, пока не починится.
Второе выглядит странно, но смысл такой: раз мы не знаем правил, считаем, что
нам запрещено. Радар в этом случае не собирает домен и пишет причину в отчёт
прогона — тишины не будет.
"""

from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser


def _allow_all() -> RobotFileParser:
    """Разбор для случая «правил нет».

    Тонкость стандартной библиотеки: свежесозданный RobotFileParser запрещает
    всё, пока в него не загрузили файл. Логика такая: правил мы не читали,
    значит, не знаем, что можно. Для отсутствующего файла это неверный ответ —
    нет файла означает «ограничений нет», — поэтому разрешаем явно.
    Поймано живым запуском на rodnik.bz, у которого robots.txt нет.
    """
    parser = RobotFileParser()
    parser.allow_all = True
    return parser


class Robots:
    """Кэш правил по доменам на один прогон."""

    def __init__(self, fetch, user_agent: str):
        self._fetch = fetch
        self._ua = user_agent
        self._cache: dict[str, tuple[RobotFileParser | None, str]] = {}

    def _for_host(self, url: str) -> tuple[RobotFileParser | None, str]:
        parts = urlparse(url)
        host = parts.netloc
        if host in self._cache:
            return self._cache[host]

        res = self._fetch(f"{parts.scheme}://{host}/robots.txt")
        status = res.get("status")
        text = res.get("text") or ""
        ctype = (res.get("content_type") or "").lower()

        if status is None:
            state = (None, "сервер не ответил")
        elif status >= 500:
            state = (None, f"сервер вернул {status}")
        elif status != 200:
            # 404 и прочие «файла нет» — ограничений нет.
            state = (_allow_all(), "файла нет")
        elif "html" in ctype or text.lstrip().startswith("<"):
            # Сайты-одностраничники (phonix.pro, rodnik.bz) отдают свою заглушку
            # на любой адрес, включая /robots.txt. Это не правила, а страница сайта.
            state = (_allow_all(), "файла нет (сайт отдал страницу)")
        else:
            parser = RobotFileParser()
            parser.parse(text.splitlines())
            state = (parser, "прочитан")

        self._cache[host] = state
        return state

    def allowed(self, url: str) -> tuple[bool, str]:
        """Можно ли забирать этот адрес. Второе значение — причина для отчёта."""
        parser, why = self._for_host(url)
        if parser is None:
            return False, f"robots.txt недоступен: {why}"
        if not parser.can_fetch(self._ua, url):
            return False, "закрыт в robots.txt"
        return True, why

    def crawl_delay(self, url: str) -> float | None:
        """Пауза, которую сайт просит соблюдать. Обычно её не выставляют."""
        parser, _ = self._for_host(url)
        if parser is None:
            return None
        try:
            value = parser.crawl_delay(self._ua)
        except Exception:
            return None
        return float(value) if value is not None else None
