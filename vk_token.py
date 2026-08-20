#!/usr/bin/env python3
"""Получение и обновление пользовательского токена ВКонтакте (VK ID, OAuth 2.1 + PKCE).

Нужен ровно для одного: метод wall.get работает ТОЛЬКО с пользовательским ключом
доступа. Сервисный ключ приложения отдаёт ошибку 28 ("method is unavailable with
service token"), ключ сообщества — "method is unavailable with group auth".

Только стандартная библиотека: ставить ничего не нужно.

Команды:
    python vk_token.py auth <APP_ID> [scope]   первичная авторизация (один раз, руками)
    python vk_token.py refresh         обновить access_token (перед каждым сбором)
    python vk_token.py test            проверить, что wall.get реально отвечает
    python vk_token.py show            показать текущий access_token

Токены лежат в vk_tokens.json рядом с этим файлом. Это секрет: в git не класть.
"""

import base64
import hashlib
import json
import os
import secrets
import ssl
import sys
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

# http://localhost без порта — единственный redirect, который VK ID пускает без
# подтверждения владения доменом. Страница по нему не откроется, и это нормально:
# нам нужен только адрес из строки браузера.
REDIRECT_URI = "http://localhost"
SCOPE = "wall groups"
API_VERSION = "5.199"
TOKEN_ENDPOINT = "https://id.vk.com/oauth2/auth"
AUTHORIZE_ENDPOINT = "https://id.vk.com/authorize"
STORE = Path(__file__).with_name("vk_tokens.json")

# Сообщества из config/sources.yaml — на них проверяем, что токен рабочий.
TEST_DOMAINS = ["mangotelecom", "roistat", "qolio", "speechanalytics"]


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def ssl_context() -> ssl.SSLContext:
    """Корни для проверки сертификата VK.

    В хранилище Windows может не оказаться корня, которым подписан vk.com
    (Google Trust Services R1). Браузер докачивает такие корни на лету, Python —
    нет, и любой запрос падает на CERTIFICATE_VERIFY_FAILED. Берём набор корней
    из certifi; он же используется httpx, которым собирает данные сам радар.
    """
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "RadarBot/0.1 (+https://okk-ai.ru)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl_context()) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        # VK кладёт причину отказа в тело ответа, а не в HTTP-статус. Показываем как есть.
        raw = exc.read().decode(errors="replace")
        print(f"HTTP {exc.code} от {url}:\n{raw}", file=sys.stderr)
        sys.exit(1)


def load_store() -> dict:
    if not STORE.exists():
        sys.exit(f"Нет {STORE.name}. Сначала: python vk_token.py auth <APP_ID>")
    return json.loads(STORE.read_text(encoding="utf-8"))


def save_store(data: dict) -> None:
    data["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    STORE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(STORE, 0o600)


def cmd_auth(app_id: str, scope: str = SCOPE) -> None:
    verifier = b64url(secrets.token_bytes(64))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_hex(16)

    url = AUTHORIZE_ENDPOINT + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": app_id,
        "redirect_uri": REDIRECT_URI,
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })

    print("1. Открываю окно авторизации ВК. Если не открылось — скопируй ссылку:\n")
    print(url, "\n")
    print("2. Разреши доступ. Браузер уйдёт на http://localhost/?code=... и покажет")
    print("   ошибку «Не удаётся получить доступ к сайту». Это ожидаемо.")
    print("3. Скопируй ВЕСЬ адрес из адресной строки и вставь сюда.\n")
    webbrowser.open(url)

    pasted = input("Адрес из браузера: ").strip()
    query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    if "code" not in query:
        sys.exit(f"В адресе нет параметра code. Что пришло: {query}")
    if query.get("state", [""])[0] != state:
        sys.exit("state не совпал — авторизацию начать заново.")

    tokens = post_form(TOKEN_ENDPOINT, {
        "grant_type": "authorization_code",
        "code": query["code"][0],
        "code_verifier": verifier,
        "client_id": app_id,
        "device_id": query.get("device_id", [""])[0],
        "redirect_uri": REDIRECT_URI,
        "state": state,
    })
    if "access_token" not in tokens:
        sys.exit(f"VK не отдал токен: {json.dumps(tokens, ensure_ascii=False)}")

    tokens["client_id"] = app_id
    tokens.setdefault("device_id", query.get("device_id", [""])[0])
    save_store(tokens)
    print(f"\nГотово. Токены в {STORE.name} (права 600).")
    print(f"access_token живёт {tokens.get('expires_in', '?')} с, дальше — refresh.")
    print("Проверка: python vk_token.py test")


def cmd_refresh() -> None:
    store = load_store()
    tokens = post_form(TOKEN_ENDPOINT, {
        "grant_type": "refresh_token",
        "refresh_token": store["refresh_token"],
        "client_id": store["client_id"],
        "device_id": store.get("device_id", ""),
        "state": secrets.token_hex(16),
    })
    if "access_token" not in tokens:
        sys.exit(f"Обновить не вышло: {json.dumps(tokens, ensure_ascii=False)}")
    # VK ID меняет refresh_token при каждом обмене. Старый становится недействителен,
    # поэтому сохраняем новый сразу же, иначе следующий запуск потеряет доступ.
    tokens["client_id"] = store["client_id"]
    tokens.setdefault("device_id", store.get("device_id", ""))
    save_store(tokens)
    print("access_token обновлён, refresh_token перезаписан.")


def api(method: str, token: str, **params) -> dict:
    params.update({"access_token": token, "v": API_VERSION})
    return post_form(f"https://api.vk.com/method/{method}", params)


def cmd_test() -> None:
    token = load_store()["access_token"]

    who = api("users.get", token)
    if "error" in who:
        err = who["error"]
        sys.exit(f"Токен не принят VK API: [{err.get('error_code')}] {err.get('error_msg')}")
    user = who["response"][0]
    print(f"Токен рабочий, от имени: {user['first_name']} {user['last_name']} (id {user['id']})\n")

    ok = True
    for domain in TEST_DOMAINS:
        res = api("wall.get", token, domain=domain, count=1)
        if "error" in res:
            err = res["error"]
            print(f"  {domain:<18} ОШИБКА [{err.get('error_code')}] {err.get('error_msg')}")
            ok = False
            continue
        total = res["response"]["count"]
        items = res["response"]["items"]
        when = datetime.fromtimestamp(items[0]["date"], timezone.utc).strftime("%d.%m.%Y") if items else "—"
        print(f"  {domain:<18} постов: {total:<6} последний: {when}")

    print("\nwall.get работает — можно строить сбор." if ok else "\nЧасть стен недоступна, см. ошибки выше.")


def cmd_show() -> None:
    print(load_store()["access_token"])


def main() -> None:
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    cmd = args[0]
    if cmd == "auth":
        if len(args) < 2:
            sys.exit("Нужен ID приложения: python vk_token.py auth 12345678")
        # Второй аргумент — scope. Нужен, если ВК не даёт права wall: у открытых
        # стен сообществ wall.get обычно читается и базовым токеном, без прав.
        cmd_auth(args[1], args[2] if len(args) > 2 else SCOPE)
    elif cmd == "refresh":
        cmd_refresh()
    elif cmd == "test":
        cmd_test()
    elif cmd == "show":
        cmd_show()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
