# Регистрирует RemoteControl.exe как Scheduled Task с автозапуском и
# перезапуском при сбое на уровне ОС. Запускать один раз, от имени администратора.
#
# Зачем: встроенный watchdog внутри exe (while-loop в main()) убивается вместе
# с обоими процессами одним End Task / End Process Tree в Task Manager.
# Задача в Task Scheduler переживает это — Windows сама поднимет процесс заново.

$ErrorActionPreference = "Stop"
$TaskName = "RemoteControlCore"
$ExePath  = Join-Path $PSScriptRoot "dist\RemoteControl.exe"

if (-not (Test-Path $ExePath)) {
    Write-Host "Не найден $ExePath — сначала запустите build-client.ps1" -ForegroundColor Red
    exit 1
}

$Action  = New-ScheduledTaskAction -Execute $ExePath
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Principal $Principal -Force

Write-Host "Задача '$TaskName' зарегистрирована. Проверить: taskschd.msc" -ForegroundColor Green
Write-Host "Запустить прямо сейчас: Start-ScheduledTask -TaskName '$TaskName'"
