#!/usr/bin/env bash
#
# Расписание радара под macOS — Фаза 5. То же, что tools/schedule.ps1 делает под
# Windows: ставит ежедневный прогон, показывает его состояние и снимает обратно.
# Только здесь вместо планировщика задач Windows — launchd, штатный сторож
# запусков в macOS.
#
# Почему launchd, а не cron. cron в macOS формально жив, но Apple объявила его
# устаревшим ещё в 10.4, и он не умеет главного: догонять пропущенное. Ноутбук
# ночью спит или закрыт — cron просто не срабатывает, и день наблюдения пропадает
# молча. launchd в такой ситуации запускает задачу сразу после пробуждения.
# Пропущенный день — это дыра в наблюдении, которую потом ничем не закрыть.
#
# Почему не облачный планировщик. Причина та же, что в Windows-версии: секреты
# радара — доступ к ВКонтакте и токен бота — лежат на этой машине и в
# репозиторий не попадают (Фазы 1 и 4). Отдать сбор в облако значит завести те
# же секреты ещё в одном месте, где их видит чужая инфраструктура.
#
# Настройки задача берёт не отсюда, а из config/config.yaml — раздел schedule.
# Скрипт спрашивает их у самого радара командой `daily.py --plan`, чтобы время
# и пути были в одном месте, а не в двух.
#
# Запуск (из папки radar):
#
#     bash tools/schedule.sh status
#     bash tools/schedule.sh install
#     bash tools/schedule.sh run
#     bash tools/schedule.sh remove
#     bash tools/schedule.sh plist      — показать файл задачи, ничего не меняя
#
# Ключ --at задаёт местное время вместо взятого из config/config.yaml, ключ
# --python — какой интерпретатор спрашивать о настройках:
#
#     bash tools/schedule.sh install --at 11:37
#
# Чем эта версия честно отличается от Windows-версии — сказано в
# docs/RASPISANIE.md, раздел «Чего расписание не умеет». Коротко: launchd не
# умеет повторить прогон через двадцать минут после ошибки. Вместо повтора
# работает проверка здоровья: второй день без сбора — и радар сам поднимает
# тревогу.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Имя задачи в launchd. Обратный домен — соглашение Apple: так задача видна
# среди чужих и её нельзя спутать. Меняется только вместе с путём к plist ниже.
LABEL="ru.okk-ai.radar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u 2>/dev/null || echo 0)"

ACTION="${1:-status}"
shift || true
AT=""
PYTHON=""

while [ $# -gt 0 ]; do
    case "$1" in
        --at)     AT="${2:-}"; shift 2 ;;
        --python) PYTHON="${2:-}"; shift 2 ;;
        *) echo "Неизвестный ключ: $1"; exit 2 ;;
    esac
done

case "$ACTION" in
    status|install|remove|run|plist) ;;
    *) echo "Команды: status, install, run, remove, plist"; exit 2 ;;
esac

# Каким Python спрашивать настройки. Сначала окружение проекта: библиотеки
# радара стоят именно там, и задача должна запускаться тем же интерпретатором,
# иначе прогон упадёт на первом же импорте httpx. Системный python3 берётся
# только если окружения нет.
if [ -z "$PYTHON" ]; then
    if [ -x "$ROOT/.venv/bin/python" ]; then
        PYTHON="$ROOT/.venv/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        PYTHON="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
        PYTHON="$(command -v python)"
    else
        echo "Python не найден. Укажите его ключом --python <путь>."
        exit 2
    fi
fi

if ! PLAN_JSON="$("$PYTHON" "$ROOT/daily.py" --plan 2>&1)"; then
    echo "Радар не отдал настройки расписания. Проверьте:"
    echo "  $PYTHON $ROOT/daily.py --plan"
    echo "$PLAN_JSON"
    exit 2
fi

# JSON разбираем тем же Python: jq в macOS из коробки нет, а тащить его ради
# девяти полей — лишняя зависимость на ровном месте.
READER='import json,sys; print(json.load(sys.stdin)[sys.argv[1]])'

field() {
    printf '%s' "$PLAN_JSON" | "$PYTHON" -c "$READER" "$1"
}

# Пути идут в XML, а в пути пользователя может оказаться амперсанд — например,
# в папке «Работа & дом». Без экранирования файл задачи станет битым XML, и
# launchd откажется его читать с сообщением, по которому причину не угадать.
XESC='import sys, xml.sax.saxutils as x; sys.stdout.write(x.escape(sys.argv[1]))'

xesc() {
    "$PYTHON" -c "$XESC" "$1"
}

TASK="$(field task)"
JOB_PYTHON="$(field python)"
SCRIPT="$(field script)"
WORKDIR="$(field workdir)"
TIME_LOCAL="$(field time_local)"
TIME_UTC="$(field time_utc)"
UTC_OFFSET="$(field utc_offset)"
TIMEOUT_MIN="$(field timeout_min)"
DIGEST_WEEKDAY="$(field digest_weekday)"

WHEN="${AT:-$TIME_LOCAL}"
HOUR=$((10#${WHEN%%:*}))
MINUTE=$((10#${WHEN##*:}))

XML_LABEL="$(xesc "$LABEL")"
XML_PYTHON="$(xesc "$JOB_PYTHON")"
XML_SCRIPT="$(xesc "$SCRIPT")"
XML_WORKDIR="$(xesc "$WORKDIR")"

write_plist() {
    cat <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$XML_LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$XML_PYTHON</string>
        <string>$XML_SCRIPT</string>
        <string>--quiet</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$XML_WORKDIR</string>

    <!-- Местное время. launchd догоняет пропущенное: если в этот час машина
         спала, прогон случится сразу после пробуждения. -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>$HOUR</integer>
        <key>Minute</key>
        <integer>$MINUTE</integer>
    </dict>

    <!-- При загрузке задачи прогон не запускаем: установка расписания не повод
         лезть на чужие сайты прямо сейчас. -->
    <key>RunAtLoad</key>
    <false/>

    <!-- Сюда попадёт то, что случилось до того, как радар завёл свой журнал:
         не тот Python, не та папка, сломанный импорт. Свой журнал прогона
         радар пишет сам, в work/logs. -->
    <key>StandardOutPath</key>
    <string>$XML_WORKDIR/work/logs/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>$XML_WORKDIR/work/logs/launchd.log</string>

    <!-- launchd запускает задачу без окружения терминала, и Python берёт
         кодировку из системы. Русский текст и знаки «минус» и «рубль» в
         журнале должны пережить это в любом случае. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONIOENCODING</key>
        <string>utf-8</string>
    </dict>
</dict>
</plist>
PLISTEOF
}

require_macos() {
    if [ "$(uname -s 2>/dev/null || echo unknown)" != "Darwin" ]; then
        echo "Это установщик для macOS: он работает через launchd."
        echo "Под Windows расписание ставится своим установщиком — смотрите"
        echo "docs/RASPISANIE.md, раздел «Как поставить»."
        exit 2
    fi
}

job_installed() {
    launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1
}

show_status() {
    echo ""
    echo "Задача:     $TASK ($LABEL)"
    echo "Запускает:  $JOB_PYTHON $SCRIPT --quiet"
    echo "Папка:      $WORKDIR"
    echo "Время:      $TIME_LOCAL местное = $TIME_UTC UTC (машина $UTC_OFFSET)"
    echo "Сводка:     день недели $DIGEST_WEEKDAY по ISO (1 — понедельник)"
    echo "Шаг ждём:   не дольше $TIMEOUT_MIN минут"
    echo "Файл:       $PLIST"

    if [ ! -f "$PLIST" ] || ! job_installed; then
        echo ""
        echo "В launchd задачи нет. Поставить:"
        echo "  bash tools/schedule.sh install"
        return
    fi

    local info state last
    info="$(launchctl print "$DOMAIN/$LABEL" 2>/dev/null || true)"
    state="$(printf '%s' "$info" | awk -F'= ' '/^[[:space:]]*state = /{print $2; exit}')"
    last="$(printf '%s' "$info" | awk -F'= ' '/last exit code = /{print $2; exit}')"

    echo ""
    echo "В launchd:       ${state:-загружена}"
    echo "Последний код:   ${last:-прогона ещё не было}"
    echo ""
    echo "Коды возврата прогона: 0 — прошёл; 1 — шаг упал или сбор не идёт"
    echo "второй день подряд; 2 — тревогу не удалось отправить."
    echo "Что было внутри — в work/logs, здоровье сбора — командой:"
    echo "  $JOB_PYTHON daily.py --health"
}

install_job() {
    require_macos
    mkdir -p "$(dirname "$PLIST")" "$WORKDIR/work/logs"
    write_plist > "$PLIST"

    # Старую задачу снимаем перед установкой: launchd не заменяет её на лету и
    # молча оставит работать прежнюю, с прежним временем.
    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    if ! launchctl bootstrap "$DOMAIN" "$PLIST" >/dev/null 2>&1; then
        # macOS старше 10.11 команды bootstrap не знает.
        launchctl unload -w "$PLIST" >/dev/null 2>&1 || true
        launchctl load -w "$PLIST"
    fi
    launchctl enable "$DOMAIN/$LABEL" >/dev/null 2>&1 || true

    echo "Задача поставлена: $TASK, ежедневно в $WHEN."
    show_status
}

remove_job() {
    require_macos
    if [ ! -f "$PLIST" ] && ! job_installed; then
        echo "Задачи $TASK в launchd нет — снимать нечего."
        return
    fi
    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || launchctl unload -w "$PLIST" >/dev/null 2>&1 || true
    rm -f "$PLIST"
    echo "Задача $TASK снята. Снимки, диффы и журналы остались на месте."
}

run_now() {
    require_macos
    if ! job_installed; then
        echo "Задачи в launchd нет. Прогон руками: $JOB_PYTHON daily.py"
        exit 1
    fi
    launchctl kickstart -k "$DOMAIN/$LABEL" >/dev/null 2>&1 || launchctl start "$LABEL"
    echo "Прогон запущен планировщиком. Он идёт молча, вывод — в work/logs."
}

case "$ACTION" in
    status)  show_status ;;
    install) install_job ;;
    remove)  remove_job ;;
    run)     run_now ;;
    plist)   write_plist ;;
esac
