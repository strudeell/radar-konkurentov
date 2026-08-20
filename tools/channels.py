"""Сбор каналов в соцсетях: Telegram и ВКонтакте.

Почему они собираются отдельно от сайтов. У страницы сайта нам нужен весь
видимый текст. У канала — только сами публикации: их номер, дата и текст.
Всё остальное на такой странице меняется каждый час само по себе — счётчик
просмотров, «онлайн», подписи «редактировано». Если снимать канал как обычную
страницу, радар будет сообщать об изменениях каждый день, ни одно из которых
не будет настоящим.

Поэтому здесь публикации вынимаются поштучно, а счётчики просто не попадают
в снимок. По этой же причине к каналам не применяется словарь шумодава:
вырезать из них уже нечего, а дата публикации — не шум, она у поста своя
и завтра не изменится.

Telegram читается через публичную веб-версию t.me/s/<канал> — без бота и без
доступа. ВКонтакте — через официальный метод wall.get, для него нужен ключ
доступа: как его получить, написано в docs/VK-TOKEN.md.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
VK_API_VERSION = "5.199"
VK_STORE = ROOT / "vk_tokens.json"


# ─────────────────────────── Telegram ───────────────────────────

def telegram_snapshot(html: str, limit: int = 30) -> tuple[str, int]:
    """Снимок канала: по строке на публикацию. Возвращает текст и число постов."""
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message"):
        post_id = (msg.get("data-post") or "").split("/")[-1]
        node = msg.select_one(".tgme_widget_message_text")
        text = " ".join(node.get_text(" ").split()) if node else ""
        stamp = msg.select_one("a.tgme_widget_message_date time[datetime]")
        when = (stamp.get("datetime", "")[:16].replace("T", " ")) if stamp else ""
        if not text:
            # Пост без текста — картинка, видео или опрос. Факт публикации
            # всё равно записываем: он и есть сигнал.
            kind = "фото" if msg.select_one(".tgme_widget_message_photo") else \
                   "видео" if msg.select_one(".tgme_widget_message_video") else "без текста"
            text = f"({kind})"
        try:
            order = int(post_id)
        except ValueError:
            order = 0
        posts.append((order, f"[{post_id}] {when} · {text}"))

    posts.sort(key=lambda p: p[0])
    posts = posts[-limit:]
    return "\n".join(line for _, line in posts), len(posts)


# ─────────────────────────── ВКонтакте ───────────────────────────

def vk_token() -> str | None:
    """Ключ доступа: сначала из настроек компьютера, потом из файла."""
    import os
    env = os.environ.get("VK_TOKEN", "").strip()
    if env:
        return env
    if not VK_STORE.exists():
        return None
    try:
        return json.loads(VK_STORE.read_text(encoding="utf-8")).get("access_token")
    except (json.JSONDecodeError, OSError):
        return None


def vk_refresh() -> bool:
    """Обновить ключ доступа. Он живёт час, поэтому обновляется перед сбором.

    Человек для этого не нужен. Если обновить не вышло — возвращаем «нет»,
    прогон продолжается без ВК, причина уходит в отчёт.
    """
    sys.path.insert(0, str(ROOT))
    try:
        import vk_token as vk_token_module
        vk_token_module.cmd_refresh()
        return True
    except SystemExit:
        return False
    except Exception:
        return False


def vk_call(method: str, token: str, **params) -> dict:
    import httpx
    params.update({"access_token": token, "v": VK_API_VERSION})
    try:
        r = httpx.post(f"https://api.vk.com/method/{method}", data=params, timeout=20)
        return r.json()
    except Exception as e:
        return {"error": {"error_code": -1, "error_msg": f"{type(e).__name__}: {e}"}}


def vk_snapshot(domain: str, token: str, count: int = 20) -> tuple[str, int, str | None]:
    """Снимок сообщества: по строке на публикацию.

    Возвращает текст снимка, число постов и текст ошибки, если она была.
    Ключ мог протухнуть за час — тогда обновляем его и пробуем ещё раз.
    """
    res = vk_call("wall.get", token, domain=domain, count=count)

    if "error" in res and res["error"].get("error_code") in (5, 27, 28):
        if vk_refresh():
            token = vk_token() or token
            res = vk_call("wall.get", token, domain=domain, count=count)

    if "error" in res:
        err = res["error"]
        return "", 0, f"[{err.get('error_code')}] {err.get('error_msg')}"

    items = res["response"]["items"]
    lines = []
    for post in sorted(items, key=lambda p: p.get("id", 0)):
        when = datetime.fromtimestamp(post["date"], timezone.utc).strftime("%Y-%m-%d %H:%M")
        text = " ".join((post.get("text") or "").split())
        if not text and post.get("copy_history"):
            src = post["copy_history"][0]
            text = "(репост) " + " ".join((src.get("text") or "").split())
        if not text:
            kinds = {a.get("type") for a in post.get("attachments", [])}
            text = "(" + ", ".join(sorted(kinds)) + ")" if kinds else "(без текста)"
        lines.append(f"[{post['id']}] {when} · {text}")

    return "\n".join(lines), len(lines), None
