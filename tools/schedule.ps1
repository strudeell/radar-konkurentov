<#
    Расписание радара — Фаза 5. Ставит ежедневный прогон в планировщик задач
    Windows, показывает его состояние и снимает обратно.

    Почему планировщик Windows, а не облако. Секреты радара — доступ к
    ВКонтакте и токен бота — лежат на этой машине и в репозиторий не попадают
    (Фазы 1 и 4). Отдать сбор облачному планировщику значит завести те же
    секреты ещё в одном месте, где их видит чужая инфраструктура. Плюс в плане
    отдельной строкой записаны грабли: расписание в репозитории работает только
    из ветки по умолчанию и молча не запускается из любой другой. У этого
    репозитория удалённой копии нет вовсе.

    Почему не «просто добавить в автозагрузку». Автозагрузка срабатывает при
    входе в систему, то есть в непредсказуемое время и по нескольку раз в день.
    Планировщик умеет то, ради чего всё затевалось: запуск в заданное время,
    догоняющий запуск после включения машины и повтор при сбое.

    Настройки задача берёт не отсюда, а из config.yaml — раздел schedule.
    Скрипт спрашивает их у самого радара командой `daily.py --plan`, чтобы
    время и пути были в одном месте, а не в двух.

    Запуск (из папки radar):

        powershell -ExecutionPolicy Bypass -File tools\schedule.ps1 -Action status
        powershell -ExecutionPolicy Bypass -File tools\schedule.ps1 -Action install
        powershell -ExecutionPolicy Bypass -File tools\schedule.ps1 -Action run
        powershell -ExecutionPolicy Bypass -File tools\schedule.ps1 -Action remove

    Ключ -At задаёт местное время вместо взятого из config.yaml:

        ... -Action install -At 11:37
#>

param(
    [ValidateSet('status', 'install', 'remove', 'run')]
    [string]$Action = 'status',
    [string]$At,
    [string]$Python
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot

# Каким Python спрашивать настройки. Задача потом запускается тем, который
# укажет сам радар: рядом с python.exe обычно лежит pythonw.exe, и он работает
# молча, без чёрного окна поверх работы раз в сутки.
if ([string]::IsNullOrWhiteSpace($Python)) {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $found) {
        Write-Host "Python не найден в PATH. Укажите его ключом -Python <путь к python.exe>."
        exit 2
    }
    $Python = $found.Source
}

$planJson = & $Python "$root\daily.py" --plan
if ($LASTEXITCODE -ne 0) {
    Write-Host "Радар не отдал настройки расписания. Проверьте: $Python $root\daily.py --plan"
    exit 2
}
$plan = $planJson | ConvertFrom-Json

$time = $plan.time_local
if (-not [string]::IsNullOrWhiteSpace($At)) { $time = $At }

function Get-RadarTask {
    try {
        return Get-ScheduledTask -TaskName $plan.task -ErrorAction Stop
    } catch {
        return $null
    }
}

function Show-Status {
    Write-Host ""
    Write-Host "Задача:     $($plan.task)"
    Write-Host "Запускает:  $($plan.python) $($plan.script) --quiet"
    Write-Host "Папка:      $($plan.workdir)"
    Write-Host "Время:      $($plan.time_local) местное = $($plan.time_utc) UTC (машина $($plan.utc_offset))"
    Write-Host "Сводка:     день недели $($plan.digest_weekday) по ISO (1 — понедельник)"

    $task = Get-RadarTask
    if ($null -eq $task) {
        Write-Host ""
        Write-Host "В планировщике задачи нет. Поставить:"
        Write-Host "  powershell -ExecutionPolicy Bypass -File tools\schedule.ps1 -Action install"
        return
    }

    $info = Get-ScheduledTaskInfo -TaskName $plan.task
    Write-Host ""
    Write-Host "В планировщике:  $($task.State)"
    Write-Host "Последний пуск:  $($info.LastRunTime), код возврата $($info.LastTaskResult)"
    Write-Host "Следующий пуск:  $($info.NextRunTime)"
    Write-Host ""
    Write-Host "Коды возврата прогона: 0 — прошёл; 1 — шаг упал или сбор не идёт"
    Write-Host "второй день подряд; 2 — тревогу не удалось отправить."
    Write-Host "Что было внутри — в logs\<дата>.log, здоровье сбора — командой:"
    Write-Host "  python daily.py --health"
}

function Install-Task {
    $when = [datetime]::ParseExact($time, 'HH:mm', $null)

    $action = New-ScheduledTaskAction -Execute $plan.python `
        -Argument ('"{0}" --quiet' -f $plan.script) -WorkingDirectory $plan.workdir

    $trigger = New-ScheduledTaskTrigger -Daily -At $when

    # StartWhenAvailable — то, ради чего вообще нужен планировщик, а не таймер:
    # если машина в 11:37 была выключена, прогон случится сразу после включения,
    # а не пропадёт. Пропущенный день — это дыра в наблюдении, которую потом
    # ничем не закрыть.
    # RestartCount — вторая попытка через 20 минут, если прогон вернул ошибку:
    # чаще всего это лежащая сеть, и через двадцать минут она уже поднялась.
    # IgnoreNew — если прошлый прогон ещё идёт, новый не запускается: два обхода
    # сразу вдвое учащают стук в чужие сайты.
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes ($plan.timeout_min * 2)) `
        -RestartCount 2 `
        -RestartInterval (New-TimeSpan -Minutes 20)

    # Задача работает от имени владельца и только когда он вошёл в систему.
    # Иначе Windows потребует сохранить пароль учётной записи — ещё один
    # секрет на диске ради того, чтобы радар собирал страницы, пока никого нет.
    # Цена решения честная: если в компьютер не входили сутки, прогон подождёт
    # до входа. Об этом сказано в RASPISANIE.md.
    $principal = New-ScheduledTaskPrincipal `
        -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName $plan.task -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force `
        -Description "Радар конкурентов: ежедневный сбор, разбор, уведомления и недельная сводка по понедельникам. Настройки — config.yaml, раздел schedule." | Out-Null

    Write-Host "Задача поставлена: $($plan.task), ежедневно в $time."
    Show-Status
}

function Remove-Task {
    $task = Get-RadarTask
    if ($null -eq $task) {
        Write-Host "Задачи $($plan.task) в планировщике нет — снимать нечего."
        return
    }
    Unregister-ScheduledTask -TaskName $plan.task -Confirm:$false
    Write-Host "Задача $($plan.task) снята. Снимки, диффы и журналы остались на месте."
}

function Start-Now {
    $task = Get-RadarTask
    if ($null -eq $task) {
        Write-Host "Задачи в планировщике нет. Прогон руками: python daily.py"
        exit 1
    }
    Start-ScheduledTask -TaskName $plan.task
    Write-Host "Прогон запущен планировщиком. Он идёт молча, вывод — в logs\<дата>.log."
}

switch ($Action) {
    'status'  { Show-Status }
    'install' { Install-Task }
    'remove'  { Remove-Task }
    'run'     { Start-Now }
}
