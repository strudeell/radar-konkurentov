"""Проверка токена ВКонтакте на всех сообществах из config/sources.yaml.

Запуск (токен берётся из переменной окружения, в файлы его не кладём):

    $env:VK_TOKEN = "ваш_сервисный_ключ"
    python tools/check_vk.py

Что проверяем:
1. Токен вообще принимается.
2. Метод wall.get отдаёт посты по каждому сообществу.
3. Токен именно сервисный (долгоживущий), а не суточный от мини-приложения.

Третий пункт — прямо из плана: «токены мини-приложений живут сутки; для фонового
сбора нужен тип приложения, дающий долгий токен. Проверить ДО того, как строить
расписание». Проверяем сейчас, а не через месяц по тишине в дайджесте.
"""

import os
import sys
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.vk.com/method/"
VERSION = "5.199"

# Коды ошибок ВК, которые означают проблему с самим токеном, а не с сообществом.
TOKEN_ERRORS = {
    5: "токен недействителен или протух",
    27: "ключ доступа сообщества недействителен",
    28: "ключ доступа приложения недействителен",
    15: "доступ запрещён (закрытое сообщество или метод недоступен сервисному ключу)",
}


def call(method: str, token: str, **params):
    r = httpx.get(f"{API}{method}", params={**params, "access_token": token,
                                            "v": VERSION}, timeout=20)
    return r.json()


def main():
    token = os.environ.get("VK_TOKEN", "").strip()
    if not token:
        print("Не задана переменная окружения VK_TOKEN.\n"
              'PowerShell:  $env:VK_TOKEN = "ваш_ключ"')
        return 1

    doc = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
    groups = [(c["name"], ch["id"]) for c in doc["competitors"]
              for ch in c["channels"] if ch["net"] == "vk"]
    if not groups:
        print("В config/sources.yaml нет сообществ ВК.")
        return 1

    print(f"Проверяю {len(groups)} сообществ.\n")
    ok = True
    for name, domain in groups:
        res = call("wall.get", token, domain=domain, count=3)
        if "error" in res:
            code = res["error"]["error_code"]
            msg = TOKEN_ERRORS.get(code, res["error"].get("error_msg", ""))
            print(f"  ОШИБКА  {name:22} vk.com/{domain:20} [{code}] {msg}")
            ok = False
            continue
        items = res["response"]["items"]
        total = res["response"]["count"]
        last = items[0]["text"][:60].replace("\n", " ") if items else "(нет постов)"
        print(f"  ok      {name:22} vk.com/{domain:20} всего постов: {total}")
        print(f"          последний: {last}...")

    # Сервисный ключ не привязан к пользователю: users.get без параметров
    # вернёт ошибку. Пользовательский токен вернёт данные владельца.
    who = call("users.get", token)
    if "error" in who:
        print("\nТип токена: похоже на СЕРВИСНЫЙ ключ — это то, что нужно.")
        print("Он не протухает через сутки и годится для фонового сбора.")
    else:
        print("\nВНИМАНИЕ: токен привязан к пользователю "
              f"({who.get('response')}). Пользовательские токены и токены "
              "мини-приложений живут ограниченное время — расписание на таком "
              "токене однажды молча перестанет собирать ВК.")
        print("Возьмите сервисный ключ доступа в настройках приложения.")
        ok = False

    print("\nИтог:", "всё готово к Фазе 2." if ok else "есть что поправить, см. выше.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
