# Remote Control — Setup
# Создаёт .venv, ставит зависимости, генерирует config.json
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

Write-Host ""
Write-Host "  Remote Control — Setup" -ForegroundColor Cyan
Write-Host "  ========================" -ForegroundColor Cyan
Write-Host ""

# ── Запрашиваем данные у пользователя ────────────────────────────────────────
$SecretKey = Read-Host "  Enter SECRET_KEY (same as on your server)"
if (-not $SecretKey.Trim()) {
    Write-Host "  ERROR: SECRET_KEY cannot be empty." -ForegroundColor Red
    exit 1
}

$UpdateUrl = Read-Host "  Enter UPDATE_URL (GitHub raw URL, or leave empty to skip)"
$PcName    = Read-Host "  Enter PC name (or leave empty — app will ask on first run)"

Write-Host ""

# ── Пишем config.json ─────────────────────────────────────────────────────────
$ConfigPath = Join-Path $Root "config.json"
$ConfigJson = @"
{
  "name": "$($PcName.Trim())",
  "server_url": "https://remote-server-mr8v.onrender.com",
  "secret_key": "$($SecretKey.Trim())",
  "update_url": "$($UpdateUrl.Trim())",
  "voice": {
    "enabled": true,
    "language": "en-US",
    "mic_index": null,
    "timeout": 8,
    "phrase_time_limit": 12,
    "ambient_duration": 1.0,
    "energy_threshold": null,
    "pause_threshold": 0.8,
    "dynamic_energy": true,
    "tts_rate": 165,
    "tts_voice_id": null
  }
}
"@
[System.IO.File]::WriteAllText($ConfigPath, $ConfigJson, [System.Text.Encoding]::UTF8)
Write-Host "  config.json created." -ForegroundColor Green

# ── Ищем Python 3.12 ──────────────────────────────────────────────────────────
$Py312 = $null
$uvPython312 = Join-Path $env:LOCALAPPDATA "uv\python\cpython-3.12.13-windows-x86_64-none\python.exe"
foreach ($candidate in @(
    "py -3.12",
    $uvPython312
)) {
    try {
        if ($candidate -like "*\*") {
            if (Test-Path $candidate) { $Py312 = $candidate; break }
        } else {
            $v = Invoke-Expression "$candidate --version 2>&1"
            if ($v -match "3\.12") { $Py312 = $candidate; break }
        }
    } catch {}
}

if (-not $Py312) {
    Write-Host "  ERROR: Python 3.12 not found!" -ForegroundColor Red
    Write-Host "  Download: https://www.python.org/downloads/release/python-31213/" -ForegroundColor Yellow
    exit 1
}

Write-Host "  Python found: $Py312" -ForegroundColor Cyan

# ── Создаём venv ──────────────────────────────────────────────────────────────
$venv = Join-Path $Root ".venv"
Write-Host "  Creating virtual environment..."
if ($Py312 -like "*\*") {
    & $Py312 -m venv $venv
} else {
    Invoke-Expression "$Py312 -m venv `"$venv`""
}

$python = Join-Path $venv "Scripts\python.exe"
& $python -m pip install --upgrade pip wheel setuptools -q

# ── Ставим зависимости ────────────────────────────────────────────────────────
Write-Host "  Installing client dependencies..."
& $python -m pip install -r (Join-Path $Root "requirements-client.txt") -q

Write-Host "  Verifying imports..." -ForegroundColor Yellow
& $python -c "import websockets, psutil, pyautogui, PIL, requests, pyperclip, speech_recognition, pyttsx3, pyaudio; print('  All imports OK')"

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "  Setup complete!" -ForegroundColor Green
    Write-Host "  Now run: .\build-client.ps1" -ForegroundColor Cyan
    Write-Host ""
} else {
    Write-Host "  Some imports failed. See errors above." -ForegroundColor Red
    exit 1
}
