# Сборка RemoteControl.exe + RemoteControlPanel.exe
# Автоматически генерирует иконку через Python если её нет
# Сборка PyInstaller пишет логи в stderr; при EAP=Stop захват потоков
# прерывает скрипт на первой строке лога. Ошибки ловим через $LASTEXITCODE.
$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
$venv = Join-Path $Root ".venv\Scripts"
$python = Join-Path $venv "python.exe"
$pyi    = Join-Path $venv "pyinstaller.exe"

if (-not (Test-Path $python)) {
    Write-Host "Run setup.ps1 first" -ForegroundColor Red
    exit 1
}

# ── Генерация иконки ──────────────────────────────────────────────────────────
$IconPath = Join-Path $Root "assets\icon.ico"
if (-not (Test-Path $IconPath)) {
    Write-Host "Generating icon..." -ForegroundColor Cyan
    $null = New-Item -ItemType Directory -Force (Join-Path $Root "assets")

    $iconScript = @'
import math
from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
px = img.load()
cx, cy = S / 2, S / 2
R = S * 0.48
c1 = (99, 91, 255)
c2 = (61, 39, 176)

def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

for y in range(S):
    for x in range(S):
        dx, dy = x - cx, y - cy
        dist = math.hypot(dx, dy)
        if dist > R:
            continue
        t = max(0.0, min(1.0, (x + y) / (2 * S)))
        col = lerp(c1, c2, t)
        alpha = 255 if dist < R - 2 else int(max(0, min(1, (R - dist) / 2)) * 255)
        px[x, y] = (col[0], col[1], col[2], alpha)

draw = ImageDraw.Draw(img)
ring_outer = R * 0.62
ring_inner = R * 0.46
draw.ellipse([cx-ring_outer, cy-ring_outer, cx+ring_outer, cy+ring_outer], fill=(255,255,255,235))

bg = Image.new("RGBA", (S, S), (0,0,0,0))
bpx = bg.load()
for y in range(int(cy-ring_inner)-2, int(cy+ring_inner)+2):
    for x in range(int(cx-ring_inner)-2, int(cx+ring_inner)+2):
        if math.hypot(x-cx, y-cy) <= ring_inner:
            t = max(0.0, min(1.0, (x+y)/(2*S)))
            col = lerp(c1, c2, t)
            bpx[x, y] = (col[0], col[1], col[2], 255)
img.paste(bg, (0,0), bg)

dot_r = R * 0.22
draw.ellipse([cx-dot_r, cy-dot_r, cx+dot_r, cy+dot_r], fill=(20,18,40,255))

sizes = [16, 24, 32, 48, 64, 128, 256]
frames = [img.resize((s, s), Image.LANCZOS) for s in sizes]
import sys
frames[-1].save(sys.argv[1], format="ICO", sizes=[(s,s) for s in sizes])
print("Icon saved:", sys.argv[1])
'@

    $tmpScript = Join-Path $env:TEMP "gen_icon.py"
    [System.IO.File]::WriteAllText($tmpScript, $iconScript, [System.Text.Encoding]::UTF8)
    & $python $tmpScript $IconPath
    Remove-Item $tmpScript -Force

    if (-not (Test-Path $IconPath)) {
        Write-Host "Icon generation failed — building without icon" -ForegroundColor DarkYellow
    }
} else {
    Write-Host "Using existing icon: $IconPath" -ForegroundColor Cyan
}

$IconArgs = @()
if (Test-Path $IconPath) { $IconArgs = @("--icon", $IconPath) }

# ── Сборка ядра (headless) ────────────────────────────────────────────────────
Write-Host "Building RemoteControl.exe (headless core)..." -ForegroundColor Yellow
& $pyi --noconfirm --clean --onefile --noconsole `
    --name RemoteControl `
    @IconArgs `
    --hidden-import=pyaudio `
    --hidden-import=speech_recognition `
    --hidden-import=pyttsx3 `
    --hidden-import=PIL.ImageGrab `
    --collect-all pyttsx3 `
    (Join-Path $Root "client_final.py")

if ($LASTEXITCODE -ne 0) { exit 1 }

# ── Сборка GUI панели ─────────────────────────────────────────────────────────
Write-Host "Building RemoteControlPanel.exe (GUI)..." -ForegroundColor Yellow
& $pyi --noconfirm --clean --onefile --windowed `
    --name RemoteControlPanel `
    @IconArgs `
    (Join-Path $Root "gui_panel.py")

if ($LASTEXITCODE -eq 0) {
    # ── Чистим мусор PyInstaller ──────────────────────────────────────────────
    @("build", "__pycache__") | ForEach-Object {
        $p = Join-Path $Root $_
        if (Test-Path $p) { Remove-Item $p -Recurse -Force }
    }
    Get-ChildItem $Root -Filter "*.spec" | Remove-Item -Force

    if (Test-Path $IconPath) {
        Copy-Item $IconPath (Join-Path $Root "dist\icon.ico") -Force
    }

    # ── Копируем plugins/ в dist/ ─────────────────────────────────────────────
    $PluginsSrc = Join-Path $Root "plugins"
    $PluginsDst = Join-Path $Root "dist\plugins"
    if (Test-Path $PluginsSrc) {
        Write-Host "Copying plugins folder..." -ForegroundColor Cyan
        if (Test-Path $PluginsDst) { Remove-Item $PluginsDst -Recurse -Force }
        Copy-Item $PluginsSrc $PluginsDst -Recurse
    }

    # ── Создаём шаблон config.json для юзеров (без секрета) ──────────────────
    # Всегда перезаписываем из config.example.json — в релиз не должен попасть
    # ни реальный secret_key, ни старый локальный config.json
    $ReleaseCfg = Join-Path $Root "dist\config.json"
    Copy-Item (Join-Path $Root "config.example.json") $ReleaseCfg -Force

    # ── Собираем zip для GitHub Release ───────────────────────────────────────
    Write-Host "Creating release zip..." -ForegroundColor Cyan
    $ZipPath = Join-Path $Root "dist\RemoteControl-release.zip"
    if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }

    $filesToZip = @(
        (Join-Path $Root "dist\RemoteControl.exe"),
        (Join-Path $Root "dist\RemoteControlPanel.exe"),
        (Join-Path $Root "dist\config.json")
    )
    if (Test-Path $IconPath) { $filesToZip += (Join-Path $Root "dist\icon.ico") }

    # Создаём временную папку со структурой
    $TmpZip = Join-Path $env:TEMP "rcrelease"
    if (Test-Path $TmpZip) { Remove-Item $TmpZip -Recurse -Force }
    $null = New-Item -ItemType Directory $TmpZip

    foreach ($f in $filesToZip) {
        if (Test-Path $f) { Copy-Item $f $TmpZip }
    }
    if (Test-Path $PluginsDst) {
        Copy-Item $PluginsDst (Join-Path $TmpZip "plugins") -Recurse
    }

    # Встраиваем иконку в exe через ResourceHacker если есть,
    # иначе иконка уже зашита PyInstaller-ом через --icon флаг
    Compress-Archive -Path (Join-Path $TmpZip "*") -DestinationPath $ZipPath
    Remove-Item $TmpZip -Recurse -Force

    Write-Host ""
    Write-Host "Done!" -ForegroundColor Green
    Write-Host ""
    Write-Host "  dist\RemoteControl.exe          <- ядро"
    Write-Host "  dist\RemoteControlPanel.exe     <- GUI панель"
    Write-Host "  dist\plugins\                   <- папка плагинов"
    Write-Host "  dist\RemoteControl-release.zip  <- ЭТО заливайте на GitHub Release"
    Write-Host ""
    Write-Host "GitHub Release:" -ForegroundColor Cyan
    Write-Host "  1. Откройте github.com/look-afk/pcrc-versions/releases/new"
    Write-Host "  2. Tag: v<номер>"
    Write-Host "  3. Прикрепите dist\RemoteControl-release.zip"
    Write-Host "  4. Обновите version.json"
} else {
    exit 1
}
