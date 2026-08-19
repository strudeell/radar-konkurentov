#!/usr/bin/env python3
"""Отправка сообщений в Telegram — единственное место, где радар пишет наружу.

Почему бот у радара отдельный, а не общий с другими задачами. У разных задач
разная критичность, разные права и разная частота отказов. Общий бот означает,
что поломка в соседней задаче — исчерпанный лимит, отозванный токен, блокировка
за спам — гасит алерты о ценах конкурентов. Радар должен ломаться сам по себе.

Где лежит доступ. Токен бота — секрет того же порядка, что и доступ к ВК: кто
его получит, тот пишет в чат владельца от лица бота. Поэтому он живёт там же,
где и вкшный, — в переменной окружения или в закрытом от git файле:

    TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID   переменные окружения, старше всего
    telegram_tokens.json                     рядом с notify.py, закрыт .gitignore

В самой программе токена нет и не будет. В сообщениях и в отчётах он показывается
только огрызком («8123456789:AAF…»): журнал отправок читают глазами и пересылают,
и полный токен в нём — та же утечка, что и в git.

Что здесь делается ещё, кроме самой отправки.

**Длинные сообщения режутся.** Telegram не принимает больше 4096 символов и
отвечает ошибкой на всё сообщение целиком. Недельная сводка по девятнадцати
конкурентам в этот размер не всегда влезает, поэтому она режется по границам
абзацев и уходит несколькими частями. Резать по буквам нельзя: разрыв посреди
HTML-разметки Telegram не примет.

**Ошибки не глотаются.** Не отправилось — программа скажет, что именно ответил
Telegram, и вернёт ненулевой код возврата. Расписание Фазы 5 по нему поймёт,
что сводка не дошла. Тихий сбой отправки хуже отсутствия отправки: человек
считает, что у конкурентов ничего не происходит.
"""

import html
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

API = "https://api.telegram.org"

# Telegram обрывает сообщение длиннее 4096 символов. Берём с запасом: длина
# считается по видимым символам, а HTML-разметка в этот счёт не входит.
LIMIT = 3900

STORE_NAME = "telegram_tokens.json"


class TelegramError(RuntimeError):
    """Telegram не принял сообщение. Текст ошибки — то, что ответил он сам."""


def escape(text: str) -> str:
    """Текст с чужого сайта внутри HTML-разметки сообщения.

    Обязательно: в заголовках конкурентов встречается «Сравнение <b>до</b> и
    после», и без экранирования Telegram отвечает «can't parse entities» на всё
    сообщение — то есть теряется не строка, а весь алерт.
    """
    return html.escape(str(text), quote=False)


@dataclass
class Bot:
    """Бот радара: токен, чат владельца и правила вежливого повтора."""
    token: str
    chat_id: str
    source: str = ""          # откуда взялся доступ — для отчёта, не для отладки
    timeout: float = 20.0
    retries: int = 2

    @property
    def masked(self) -> str:
        """Токен в виде, который не страшно записать в журнал."""
        head, _, tail = self.token.partition(":")
        return f"{head}:{tail[:3]}…" if tail else "…"

    def _call(self, method: str, payload: dict) -> dict:
        url = f"{API}/bot{self.token}/{method}"
        last = ""
        for attempt in range(self.retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    answer = client.post(url, json=payload)
            except httpx.RequestError as error:
                last = f"сеть: {error.__class__.__name__}"
                time.sleep(2 * (attempt + 1))
                continue

            try:
                body = answer.json()
            except json.JSONDecodeError:
                last = f"HTTP {answer.status_code}, ответ не разобрался"
                time.sleep(2 * (attempt + 1))
                continue

            if body.get("ok"):
                return body["result"]

            # 429: слишком часто. Telegram сам говорит, сколько ждать, и это
            # не ошибка, а просьба. Ждём столько, сколько просит, и повторяем.
            if answer.status_code == 429:
                wait = int((body.get("parameters") or {}).get("retry_after", 5))
                last = f"слишком часто, Telegram просит подождать {wait} с"
                time.sleep(min(wait, 60))
                continue

            # Остальные ошибки повторять бессмысленно: неверный токен, бот не
            # знает чата, чат заблокировал бота. Их надо показать человеку.
            raise TelegramError(f"{method}: {body.get('description', 'без описания')} "
                                f"(HTTP {answer.status_code})")
        raise TelegramError(f"{method}: {last}")

    def me(self) -> dict:
        """Кто мы: имя бота. Проверка, что токен живой."""
        return self._call("getMe", {})

    def send(self, text: str, chat_id: str | None = None,
             silent: bool = False) -> list[int]:
        """Отправить сообщение, при необходимости разрезав его на части."""
        parts = split(text)
        sent = []
        for number, part in enumerate(parts, 1):
            tail = ""
            if len(parts) > 1:
                tail = f"\n\n<i>часть {number} из {len(parts)}</i>"
            result = self._call("sendMessage", {
                "chat_id": chat_id or self.chat_id,
                "text": part + tail,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": silent,
            })
            sent.append(result.get("message_id"))
            if number < len(parts):
                time.sleep(1)     # части одного сообщения не гоним очередью
        return sent


def split(text: str, limit: int = LIMIT) -> list[str]:
    """Разрезать длинный текст по границам абзацев, а если нечем — по строкам.

    Границы важнее размера: часть сводки должна начинаться с имени конкурента,
    а не с середины его строки. Поэтому сначала абзацы, потом строки, и только
    если одна строка длиннее лимита целиком — режем её по буквам.
    """
    if len(text) <= limit:
        return [text]

    parts, current = [], ""
    for block in text.split("\n\n"):
        for piece in _fit(block, limit):
            if not current:
                current = piece
            elif len(current) + 2 + len(piece) <= limit:
                current = f"{current}\n\n{piece}"
            else:
                parts.append(current)
                current = piece
    if current:
        parts.append(current)
    return parts


def _fit(block: str, limit: int) -> list[str]:
    """Разбить один абзац, если он сам по себе длиннее лимита."""
    if len(block) <= limit:
        return [block]
    out, current = [], ""
    for line in block.split("\n"):
        while len(line) > limit:
            out.append(line[:limit])
            line = line[limit:]
        if not current:
            current = line
        elif len(current) + 1 + len(line) <= limit:
            current = f"{current}\n{line}"
        else:
            out.append(current)
            current = line
    if current:
        out.append(current)
    return out


def load(root: Path, env: dict | None = None) -> Bot | None:
    """Достать доступ к боту. Переменные окружения старше файла.

    Порядок именно такой: на рабочей машине удобнее файл, а в расписании
    Фазы 5 — переменные окружения, и они должны перебивать залежавшийся файл.
    Ничего не нашлось — возвращается None, и это не ошибка: без настроенного
    бота радар работает и просто показывает сообщения на экране.
    """
    env = os.environ if env is None else env

    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (env.get("TELEGRAM_CHAT_ID") or "").strip()
    source = "переменные окружения"

    if not token:
        store = root / STORE_NAME
        if not store.exists():
            return None
        try:
            saved = json.loads(store.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            raise TelegramError(f"{STORE_NAME} не читается: {error}") from error
        token = str(saved.get("bot_token") or "").strip()
        chat = chat or str(saved.get("chat_id") or "").strip()
        source = STORE_NAME

    if not token:
        return None
    if not chat:
        raise TelegramError(
            "токен бота есть, а чат не указан: заполните chat_id в "
            f"{STORE_NAME} или переменную TELEGRAM_CHAT_ID. Как узнать номер "
            "чата — в TELEGRAM-BOT.md")
    return Bot(token=token, chat_id=chat, source=source)
