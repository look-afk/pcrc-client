"""
Remote Control Client — финальная версия
Все команды работают стабильно. Голос всегда активен.
"""

import asyncio, json, os, sys, subprocess, shutil, base64, io
import threading, logging, platform, socket, tempfile, multiprocessing
import queue as _queue, time as _time, importlib
from datetime import datetime
from pathlib import Path

# Добавляем папку приложения в sys.path чтобы plugins/ был доступен
_app_dir_for_path = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
if str(_app_dir_for_path) not in sys.path:
    sys.path.insert(0, str(_app_dir_for_path))

# ── Автоустановка зависимостей ──
# Обязательные — без них клиент не стартует.
# Голосовые (pyaudio, pyttsx3) — опциональны; на Windows голос работает через SAPI.
_REQUIRED_DEPS = [
    ("websockets", "websockets"),
    ("psutil", "psutil"),
    ("pyautogui", "pyautogui"),
    ("Pillow", "PIL"),
    ("requests", "requests"),
    ("pyperclip", "pyperclip"),
]
_OPTIONAL_DEPS = [
    ("SpeechRecognition", "speech_recognition"),
    ("pyttsx3", "pyttsx3"),
    ("pyaudio", "pyaudio"),
]

def _pip(pkg: str) -> bool:
    """Попытка pip install. Возвращает True при успехе."""
    flags = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
    attempts = [
        [sys.executable, "-m", "pip", "install", pkg, "-q"],
        [sys.executable, "-m", "pip", "install", pkg, "-q", "--break-system-packages"],
        [sys.executable, "-m", "pip", "install", pkg, "-q", "--user"],
    ]
    for cmd in attempts:
        try:
            subprocess.check_call(cmd, creationflags=flags)
            return True
        except subprocess.CalledProcessError:
            continue
    return False

def _ensure_module(pip_name: str, import_name: str, *, required: bool) -> bool:
    try:
        importlib.import_module(import_name)
        return True
    except ImportError:
        print(f"[RC] Installing {pip_name}...")
        if not _pip(pip_name):
            if required:
                print(f"[RC] FATAL: cannot install {pip_name}. Run: pip install {pip_name}")
                sys.exit(1)
            print(f"[RC] WARN: {pip_name} not installed — optional feature disabled")
            return False
        try:
            importlib.import_module(import_name)
            return True
        except ImportError:
            if required:
                print(f"[RC] FATAL: {pip_name} installed but import failed")
                sys.exit(1)
            print(f"[RC] WARN: {pip_name} import failed — optional feature disabled")
            return False

def bootstrap_dependencies() -> None:
    for pip_name, import_name in _REQUIRED_DEPS:
        _ensure_module(pip_name, import_name, required=True)
    for pip_name, import_name in _OPTIONAL_DEPS:
        _ensure_module(pip_name, import_name, required=False)

bootstrap_dependencies()

import websockets
import psutil
import pyautogui
import requests
import pyperclip
import tkinter as tk
from tkinter import font as tkfont
from PIL import Image, ImageGrab

# Plugin system (загружается после конфига в _worker_main)
try:
    from plugins.manager import plugin_manager
    _PLUGINS_AVAILABLE = True
except ImportError:
    plugin_manager = None
    _PLUGINS_AVAILABLE = False

# Опциональные модули голоса
sr = None
pyttsx3 = None
HAS_SR = HAS_TTS = HAS_PYAUDIO = False

try:
    import speech_recognition as sr
    HAS_SR = True
except ImportError:
    pass

try:
    import pyttsx3
    HAS_TTS = True
except ImportError:
    pass

try:
    import pyaudio  # noqa: F401
    HAS_PYAUDIO = True
except ImportError:
    pass

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.05

# ── Paths ──
APP_DIR     = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
CONFIG_FILE = APP_DIR / "config.json"
_LOG_DIR    = Path(os.environ.get("LOCALAPPDATA", str(APP_DIR))) / "RemoteControl"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE    = _LOG_DIR / "client.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("rc")

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

# ── Version ──
CLIENT_VERSION   = 5
DEFAULT_UPDATE_URL = ""

# Ed25519-публичный ключ для подписи метаданных обновления (version.json "sig").
# Приватный ключ: .signing/update_signing_private.pem (в .gitignore, НЕ коммитить).
# Переподпись: python sign-version.py <version.json>
UPDATE_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEA2n9itFm+W9uWsAcrDJPhejecyNgXt2ZFTtpQwuVJM2Q=
-----END PUBLIC KEY-----"""

# ── Config ──
DEFAULT_CFG = {
    "name": "",
    "server_url": "https://remote-server-mr8v.onrender.com",
    "secret_key": "",   # НЕ хардкодить! Задаётся только в локальном config.json (в .gitignore)
    "update_url": "",
    "voice": {
        "enabled": True,
        "language": "en-US",
        "mic_index": None,
        "timeout": 8,
        "phrase_time_limit": 12,
        "ambient_duration": 1.0,
        "energy_threshold": None,
        "pause_threshold": 0.8,
        "dynamic_energy": True,
        "tts_rate": 165,
        "tts_voice_id": None,
    },
}

def load_config():
    if CONFIG_FILE.exists():
        try:
            return {**DEFAULT_CFG, **json.loads(CONFIG_FILE.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return DEFAULT_CFG.copy()

def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

def normalize_ws_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    if url.startswith(("ws://", "wss://")) and not url.endswith("/ws"):
        url += "/ws"
    return url

def ask_name_once():
    """Show a nice popup to ask for PC name."""
    result = {"name": ""}
    def submit():
        v = entry.get().strip()
        if v:
            result["name"] = v
            root.destroy()
        else:
            entry.config(highlightbackground="red", highlightthickness=2)

    root = tk.Tk()
    root.title("Remote Control — Setup")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()
    w, h = 380, 170
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
    root.configure(bg="#0d1117")

    tk.Label(root, text="Remote Control — First Setup",
             bg="#0d1117", fg="#e6edf3",
             font=("Segoe UI", 12, "bold")).pack(pady=(18, 4))
    tk.Label(root, text="Enter a name for this PC  (e.g.  home_pc,  office_laptop)",
             bg="#0d1117", fg="#8b949e",
             font=("Segoe UI", 9)).pack()

    entry = tk.Entry(root, width=30, bg="#161b22", fg="#e6edf3",
                     insertbackground="#e6edf3", relief="flat",
                     font=("Segoe UI", 11),
                     highlightthickness=1, highlightbackground="#30363d")
    entry.pack(pady=10, ipady=5)
    entry.focus_set()
    entry.bind("<Return>", lambda e: submit())

    tk.Button(root, text="Save & Start", command=submit,
              bg="#58a6ff", fg="#0d1117", relief="flat",
              font=("Segoe UI", 10, "bold"),
              padx=16, pady=5, cursor="hand2").pack()

    root.protocol("WM_DELETE_WINDOW", lambda: None)
    root.mainloop()
    return result["name"]

# ── Globals set at runtime ──
CFG          = {}
_CWD         = Path.home()
_ws_connected = False      # updated by ws_loop

# ── Helpers ──
def mb(b):
    if b < 1048576: return f"{b/1024:.1f} KB"
    return f"{b/1048576:.1f} MB"

def gb(b): return f"{b/1073741824:.2f} GB"

def run(cmd, timeout=30, cwd=None):
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
            cwd=str(cwd) if cwd else None,
        )
        return (r.stdout + r.stderr).strip()[:4000] or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Timeout after {timeout}s"
    except Exception as e:
        return f"Error: {e}"

def ps(cmd, timeout=30):
    safe = cmd.replace('"', '\\"')
    return run(
        f'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "{safe}"',
        timeout
    )

def _upload(file_data: bytes, filename: str = "file.bin") -> str:
    # Method 1 — file.io
    try:
        r = requests.post("https://file.io/?expires=1d",
                          files={"file": (filename, file_data)}, timeout=30)
        if r.status_code == 200:
            d = r.json()
            if d.get("success"):
                return d["link"]
    except Exception as e:
        log.debug(f"file.io: {e}")
    # Method 2 — 0x0.st
    try:
        r = requests.post("https://0x0.st",
                          files={"file": (filename, file_data)}, timeout=30)
        if r.status_code == 200 and r.text.strip().startswith("http"):
            return r.text.strip()
    except Exception as e:
        log.debug(f"0x0.st: {e}")
    # Method 3 — transfer.sh
    try:
        r = requests.put(
            f"https://transfer.sh/{filename}",
            data=file_data,
            headers={"Max-Downloads": "10", "Max-Days": "1"},
            timeout=30,
        )
        if r.status_code == 200 and r.text.strip().startswith("http"):
            return r.text.strip()
    except Exception as e:
        log.debug(f"transfer.sh: {e}")
    raise RuntimeError("All upload methods failed")

# ─────────────────────────────────────────────────────────────────────────────
#  AUTO-UPDATE
# ─────────────────────────────────────────────────────────────────────────────
_UPDATE_STAGED  = _LOG_DIR / "pending_update.exe"
_UPDATE_ZIP     = _LOG_DIR / "pending_update.zip"
_UPDATE_VER_F   = _LOG_DIR / "pending_version.txt"
_UPDATE_ROLLBACK_EXE = _LOG_DIR / "rollback.exe"
_UPDATE_ROLLBACK_VER = _LOG_DIR / "rollback_version.txt"
_UPDATE_APPLIED_FLAG = _LOG_DIR / "update_applied.flag"
_UPDATE_CRASH_COUNT  = _LOG_DIR / "update_crash_count.txt"

def apply_pending_update() -> bool:
    """Apply staged update. Supports both .exe and .zip releases."""
    staged_zip = _UPDATE_ZIP.exists()
    staged_exe = _UPDATE_STAGED.exists()
    if not staged_zip and not staged_exe:
        return False
    if not getattr(sys, "frozen", False):
        _UPDATE_STAGED.unlink(missing_ok=True)
        _UPDATE_ZIP.unlink(missing_ok=True)
        return False
    try:
        exe = Path(sys.executable)
        exe_dir = exe.parent

        # FIX: back up the exe that is about to be replaced, and record the
        # version we are upgrading FROM, so a broken update can be rolled
        # back automatically instead of leaving the machine on a dead build.
        try:
            shutil.copy2(exe, _UPDATE_ROLLBACK_EXE)
            _UPDATE_ROLLBACK_VER.write_text(str(CLIENT_VERSION))
            log.info(f"Backed up current exe (v{CLIENT_VERSION}) before applying update")
        except Exception as e:
            log.error(f"Could not back up current exe before update — aborting update for safety: {e}")
            return False

        pending_version = _UPDATE_VER_F.read_text().strip() if _UPDATE_VER_F.exists() else "?"
        _UPDATE_APPLIED_FLAG.write_text(json.dumps({
            "from_version": CLIENT_VERSION,
            "to_version": pending_version,
            "applied_at": datetime.now().isoformat(timespec="seconds"),
        }))
        _UPDATE_CRASH_COUNT.write_text("0")

        bat_lines = [
            "@echo off",
            "timeout /t 2 /nobreak >nul",
            # Убиваем старый процесс перед заменой
            f'taskkill /IM "{exe.name}" /F >nul 2>&1',
            "timeout /t 2 /nobreak >nul",
        ]

        if staged_zip:
            # Распаковываем zip рядом с exe
            tmp_unzip = Path(tempfile.gettempdir()) / "rc_update_unzip"
            import zipfile
            with zipfile.ZipFile(_UPDATE_ZIP) as zf:
                zf.extractall(tmp_unzip)
            # Ищем RemoteControl.exe внутри распакованного
            new_exe = next(tmp_unzip.rglob("RemoteControl.exe"), None)
            if new_exe:
                bat_lines += [
                    f'copy /Y "{new_exe}" "{exe}" >nul',
                ]
            # Копируем plugins/ если есть
            new_plugins = tmp_unzip / "plugins"
            dst_plugins = exe_dir / "plugins"
            if new_plugins.exists():
                bat_lines += [
                    f'robocopy "{new_plugins}" "{dst_plugins}" /E /IS /IT /NFL /NDL /NJH /NJS >nul',
                ]
            bat_lines += [
                f'rmdir /s /q "{tmp_unzip}" >nul 2>&1',
                f'del /f /q "{_UPDATE_ZIP}" >nul',
            ]
        else:
            bat_lines += [f'copy /Y "{_UPDATE_STAGED}" "{exe}" >nul',
                          f'del /f /q "{_UPDATE_STAGED}" >nul']

        bat_lines += [
            f'del /f /q "{_UPDATE_VER_F}" >nul',
            f'start "" "{exe}"',
            'del /f /q "%~f0"',
        ]
        bat = Path(tempfile.gettempdir()) / "rc_update.bat"
        bat.write_text("\n".join(bat_lines), encoding="utf-8")
        subprocess.Popen(
            f'start "" /b cmd /c "{bat}"',
            shell=True,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0,
        )
        return True
    except Exception as e:
        log.error(f"Apply update: {e}")
        return False


def rollback_to_previous_version(reason: str) -> bool:
    """
    FIX: counterpart to apply_pending_update's new backup step. Restores the
    exe that was running before the last update and restarts into it. Used
    both by the post-update connectivity check and by the watchdog's
    crash-loop detector in main().
    """
    if not getattr(sys, "frozen", False):
        return False
    if not _UPDATE_ROLLBACK_EXE.exists():
        log.error(f"Rollback requested ({reason}) but no rollback.exe is available")
        return False
    try:
        exe = Path(sys.executable)
        rollback_ver = _UPDATE_ROLLBACK_VER.read_text().strip() if _UPDATE_ROLLBACK_VER.exists() else "previous"
        log.error(f"Rolling back to v{rollback_ver} — reason: {reason}")
        bat_lines = [
            "@echo off",
            "timeout /t 2 /nobreak >nul",
            f'taskkill /IM "{exe.name}" /F >nul 2>&1',
            "timeout /t 2 /nobreak >nul",
            f'copy /Y "{_UPDATE_ROLLBACK_EXE}" "{exe}" >nul',
            f'del /f /q "{_UPDATE_APPLIED_FLAG}" >nul 2>&1',
            f'del /f /q "{_UPDATE_CRASH_COUNT}" >nul 2>&1',
            f'start "" "{exe}"',
            'del /f /q "%~f0"',
        ]
        bat = Path(tempfile.gettempdir()) / "rc_rollback.bat"
        bat.write_text("\n".join(bat_lines), encoding="utf-8")
        subprocess.Popen(
            f'start "" /b cmd /c "{bat}"',
            shell=True,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0,
        )
        return True
    except Exception as e:
        log.error(f"Rollback failed: {e}")
        return False


async def post_update_watchdog(stop_evt: threading.Event, timeout_secs: int = 90) -> None:
    """
    FIX: runs only right after an update was applied (update_applied.flag
    present). If the client has not successfully registered with the server
    within `timeout_secs`, assume the new build is broken and roll back
    automatically, instead of silently staying disconnected forever.
    """
    if not _UPDATE_APPLIED_FLAG.exists():
        return
    log.info(f"Post-update watchdog: must connect within {timeout_secs}s or we roll back")
    waited = 0
    while waited < timeout_secs:
        if stop_evt.is_set():
            return
        if _ws_connected:
            log.info("Post-update watchdog: connected successfully — keeping this version")
            _UPDATE_APPLIED_FLAG.unlink(missing_ok=True)
            _UPDATE_CRASH_COUNT.unlink(missing_ok=True)
            # Rollback copy is no longer needed once the new version is confirmed good.
            _UPDATE_ROLLBACK_EXE.unlink(missing_ok=True)
            _UPDATE_ROLLBACK_VER.unlink(missing_ok=True)
            return
        await asyncio.sleep(2)
        waited += 2
    if rollback_to_previous_version(f"could not connect to server within {timeout_secs}s after update"):
        os._exit(1)

def _verify_sha256(data: bytes, expected: str) -> bool:
    """Проверить sha256 скачанного файла. expected — hex из version.json."""
    import hashlib
    expected = (expected or "").strip().lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        return False
    return hashlib.sha256(data).hexdigest() == expected

def _update_sig_canonical(d: dict) -> bytes:
    """Каноничные байты метаданных для Ed25519-подписи.
    Должна совпадать с sign-version.py: JSON всех полей кроме sig."""
    d2 = {k: v for k, v in d.items() if k != "sig"}
    return json.dumps(d2, sort_keys=True, separators=(",", ":")).encode("utf-8")

def _verify_update_sig(d: dict, sig_b64: str) -> bool:
    """Проверить Ed25519-подпись метаданных обновления (весь объект version.json)."""
    try:
        import base64
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = serialization.load_pem_public_key(UPDATE_PUBLIC_KEY.encode())
        if not isinstance(pub, Ed25519PublicKey):
            return False
        pub.verify(base64.b64decode(sig_b64.strip()), _update_sig_canonical(d))
        return True
    except Exception as e:
        log.debug(f"Update signature check failed: {e}")
        return False

async def check_for_update():
    """
    update_url: https://raw.githubusercontent.com/USER/REPO/main -> GET {url}/version.json

    version.json shape:
      {
        "version": 5,
        "url": ".../releases/download/v5/RemoteControl.exe",   # для СТАРЫХ клиентов (exe!)
        "sha256": "<sha256 of exe>",
        "zip_url": ".../releases/download/v5/RemoteControl-release.zip",  # для новых
        "zip_sha256": "<sha256 of zip>",
        "sig": "<base64 Ed25519 over canonical json without sig>"
      }

    Поле url ОБЯЗАНО заканчиваться .exe: старые клиенты не понимают zip
    и сломались бы. Новые клиенты берут zip_url (если есть) — с plugins/.
    sha256/sig обязательны — без них обновление не принимается.
    Returns (version, download_url, sha256, sig).
    """
    url = CFG.get("update_url", "").strip()
    if not url:
        return 0, "", "", ""
    url = url.rstrip("/")
    candidates = [f"{url}/version.json", f"{url}/version"]
    for endpoint in candidates:
        try:
            r = requests.get(endpoint, timeout=8)
            if r.status_code != 200:
                continue
            d = r.json()
            ver = int(d.get("version", 0))
            sig = str(d.get("sig", "") or "").strip()
            if ver <= CLIENT_VERSION:
                return 0, "", "", ""
            if not d.get("url") and not d.get("zip_url"):
                return 0, "", "", ""
            if not sig or not _verify_update_sig(d, sig):
                log.error(f"Update v{ver} rejected: missing/bad Ed25519 signature")
                return 0, "", "", ""
            if d.get("zip_url"):
                dl = str(d["zip_url"]).strip()
                sha = str(d.get("zip_sha256", "") or "").strip().lower()
            else:
                dl = str(d["url"]).strip()
                sha = str(d.get("sha256", "") or "").strip().lower()
            if not dl or not sha:
                log.error(f"Update v{ver} rejected: no download url/sha256 in version.json")
                return 0, "", "", ""
            return ver, dl, sha, sig
        except Exception as e:
            log.debug(f"Update check {endpoint}: {e}")
            continue
    return 0, "", "", ""

async def update_checker_loop():
    await asyncio.sleep(60)
    while True:
        try:
            ver, url, sha, _sig = await check_for_update()
            if ver and url:
                log.info(f"Update v{ver} available — downloading…")
                r = requests.get(url, timeout=120)
                if r.status_code == 200:
                    if not _verify_sha256(r.content, sha):
                        log.error(
                            "Update v%s FAILED sha256 check — discarded. "
                            "Metadata sha256='%s', actual=%s. Check version.json.",
                            ver, sha, __import__("hashlib").sha256(r.content).hexdigest(),
                        )
                    else:
                        # Определяем по URL — zip или exe
                        if url.endswith(".zip"):
                            _UPDATE_ZIP.write_bytes(r.content)
                        else:
                            _UPDATE_STAGED.write_bytes(r.content)
                        _UPDATE_VER_F.write_text(str(ver))
                        log.info(f"Update v{ver} staged — auto-applying in 60s")
                        # Авто-применение: не ждём перезагрузки/ручной команды
                        await asyncio.sleep(60)
                        if apply_pending_update():
                            os._exit(0)
                        log.error("Update auto-apply failed — will retry next cycle")
        except Exception as e:
            log.debug(f"Update loop: {e}")
        await asyncio.sleep(1800)

# ─────────────────────────────────────────────────────────────────────────────
#  COMMANDS — 89 total
# ─────────────────────────────────────────────────────────────────────────────

# 1. SCREENSHOT
def _screenshot_ctypes():
    """Take screenshot using only Windows built-in ctypes — no Pillow/pyautogui needed."""
    import ctypes
    import ctypes.wintypes as wt
    # Get screen dimensions
    SM_CXSCREEN, SM_CYSCREEN = 0, 1
    w = ctypes.windll.user32.GetSystemMetrics(SM_CXSCREEN)
    h = ctypes.windll.user32.GetSystemMetrics(SM_CYSCREEN)
    # Get device contexts
    hdc_screen = ctypes.windll.user32.GetDC(0)
    hdc_mem    = ctypes.windll.gdi32.CreateCompatibleDC(hdc_screen)
    hbmp       = ctypes.windll.gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    ctypes.windll.gdi32.SelectObject(hdc_mem, hbmp)
    ctypes.windll.gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, 0, 0, 0x00CC0020)  # SRCCOPY
    # Extract bitmap pixels
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize",          ctypes.c_uint32),
            ("biWidth",         ctypes.c_int32),
            ("biHeight",        ctypes.c_int32),
            ("biPlanes",        ctypes.c_uint16),
            ("biBitCount",      ctypes.c_uint16),
            ("biCompression",   ctypes.c_uint32),
            ("biSizeImage",     ctypes.c_uint32),
            ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32),
            ("biClrUsed",       ctypes.c_uint32),
            ("biClrImportant",  ctypes.c_uint32),
        ]
    bmi = BITMAPINFOHEADER()
    bmi.biSize      = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth     = w
    bmi.biHeight    = -h   # negative = top-down
    bmi.biPlanes    = 1
    bmi.biBitCount  = 32
    bmi.biCompression = 0  # BI_RGB
    buf_size = w * h * 4
    raw = (ctypes.c_char * buf_size)()
    ctypes.windll.gdi32.GetDIBits(hdc_mem, hbmp, 0, h, raw, ctypes.byref(bmi), 0)
    # Cleanup
    ctypes.windll.gdi32.DeleteObject(hbmp)
    ctypes.windll.gdi32.DeleteDC(hdc_mem)
    ctypes.windll.user32.ReleaseDC(0, hdc_screen)
    return bytes(raw), w, h

def _make_png(raw_bgra: bytes, w: int, h: int) -> bytes:
    """Convert raw BGRA pixels to PNG using only stdlib (zlib + struct)."""
    import zlib, struct
    # BGRA -> RGB
    rgb_rows = []
    for y in range(h):
        row = b"\x00"  # filter type None
        for x in range(w):
            i = (y * w + x) * 4
            b, g, r, a = raw_bgra[i], raw_bgra[i+1], raw_bgra[i+2], raw_bgra[i+3]
            row += bytes([r, g, b])
        rgb_rows.append(row)
    raw_data = b"".join(rgb_rows)
    compressed = zlib.compress(raw_data, 6)
    def chunk(name: bytes, data: bytes) -> bytes:
        c = name + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    png  = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", compressed)
    png += chunk(b"IEND", b"")
    return png


def _screen_metrics():
    """Размер экрана без внешних библиотек (Windows)."""
    if IS_WIN:
        import ctypes
        w = ctypes.windll.user32.GetSystemMetrics(0)
        h = ctypes.windll.user32.GetSystemMetrics(1)
        return w, h
    return pyautogui.size()

def _crop_png(png_data: bytes, x: int, y: int, w: int, h: int) -> bytes:
    img = Image.open(io.BytesIO(png_data))
    cropped = img.crop((x, y, x + w, y + h))
    buf = io.BytesIO()
    cropped.save(buf, "PNG")
    return buf.getvalue()

def _format_screenshot(png_data: bytes, width: int, height: int) -> str:
    """Загрузить на хостинг или вернуть base64 для Telegram."""
    try:
        link = _upload(png_data, "screenshot.png")
        return f"OK {width}x{height}\n{link}"
    except Exception as e:
        log.debug(f"upload failed: {e}")
    b64 = base64.b64encode(png_data).decode()
    return f"[SCREENSHOT_B64]{width}x{height}\n{b64}"

def _grab_png(region=None):
    """Захват экрана: pyautogui -> Pillow -> ctypes (Windows). region=(x,y,w,h)."""
    png_data = width = height = None

    try:
        img = pyautogui.screenshot(region=region)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        png_data = buf.getvalue()
        width, height = img.width, img.height
    except Exception as e:
        log.debug(f"pyautogui grab: {e}")

    if png_data is None:
        try:
            bbox = None
            if region:
                x, y, w, h = region
                bbox = (x, y, x + w, y + h)
            img = ImageGrab.grab(bbox=bbox)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            png_data = buf.getvalue()
            width, height = img.width, img.height
        except Exception as e:
            log.debug(f"PIL grab: {e}")

    if png_data is None and IS_WIN:
        try:
            raw, sw, sh = _screenshot_ctypes()
            full_png = _make_png(raw, sw, sh)
            if region:
                x, y, w, h = region
                png_data = _crop_png(full_png, x, y, w, h)
                width, height = w, h
            else:
                png_data = full_png
                width, height = sw, sh
        except Exception as e:
            log.debug(f"ctypes grab: {e}")

    if png_data is None:
        raise RuntimeError("Screenshot failed on this system")
    return png_data, width, height



async def cmd_screenshot(p):
    try:
        png_data, width, height = _grab_png()
        return _format_screenshot(png_data, width, height)
    except Exception as e:
        return f"Error: {e}"

async def cmd_screenshot_region(p):
    try:
        x, y, w, h = map(int, p.split())
        png_data, width, height = _grab_png(region=(x, y, w, h))
        return _format_screenshot(png_data, width, height)
    except ValueError:
        return "Usage: screenshot_region x y width height"
    except Exception as e:
        return f"Error: {e}"

async def cmd_screen_size(p):
    try:
        w, h = _screen_metrics()
        return f"Screen: {w}x{h}"
    except Exception as e:
        return f"Error: {e}"

# 2. MOUSE
async def cmd_mouse_move(p):
    try:
        x, y = map(int, p.split()); pyautogui.moveTo(x, y, duration=0.2)
        return f"Mouse -> ({x},{y})"
    except: return "Usage: mouse_move x y"

async def cmd_mouse_click(p):
    try:
        parts = p.lower().split(); x = y = None; button = "left"; clicks = 1
        for t in parts:
            if t in ("right","middle"): button = t
            elif t == "double": clicks = 2
            else:
                try:
                    n = int(t)
                    if x is None: x = n
                    else: y = n
                except: pass
        if x is not None and y is not None:
            pyautogui.click(x, y, button=button, clicks=clicks, interval=0.1)
            return f"Clicked ({x},{y}) [{button}]"
        pyautogui.click(button=button, clicks=clicks, interval=0.1)
        return f"Clicked [{button}]"
    except Exception as e: return f"Error: {e}"

async def cmd_mouse_scroll(p):
    try:
        n = int(p.strip() or "3"); pyautogui.scroll(n); return f"Scrolled {n}"
    except: return "Usage: mouse_scroll n"

async def cmd_mouse_pos(p):
    pos = pyautogui.position(); return f"Mouse: x={pos.x}, y={pos.y}"

async def cmd_mouse_drag(p):
    try:
        x1,y1,x2,y2 = map(int, p.split())
        pyautogui.moveTo(x1, y1, duration=0.2)
        pyautogui.dragTo(x2, y2, duration=0.4, button="left")
        return f"Dragged ({x1},{y1}) -> ({x2},{y2})"
    except: return "Usage: mouse_drag x1 y1 x2 y2"

# 3. KEYBOARD
async def cmd_type_text(p):
    if not p.strip(): return "Usage: type_text <text>"
    try:
        await asyncio.sleep(0.3)
        if IS_WIN:
            safe = p.replace("'", "\\'")
            run(f'powershell -NoProfile -NonInteractive -Command "Set-Clipboard -Value \'{safe}\'"')
        else:
            pyperclip.copy(p)
        await asyncio.sleep(0.15)
        pyautogui.hotkey("ctrl", "v")
        return f"Typed: {p[:60]}"
    except Exception as e: return f"Error: {e}"

async def cmd_hotkey(p):
    try:
        keys = p.strip().split()
        if not keys: return "Usage: hotkey ctrl c"
        pyautogui.hotkey(*keys); return f"Hotkey: {'+'.join(keys)}"
    except Exception as e: return f"Error: {e}"

async def cmd_press_key(p):
    try:
        key = p.strip()
        if not key: return "Usage: press_key <key>"
        pyautogui.press(key); return f"Pressed: {key}"
    except Exception as e: return f"Error: {e}"

# 4. NAVIGATION
async def cmd_pwd(p):
    return f"CWD: {_CWD}"

async def cmd_cd(p):
    global _CWD
    try:
        target = Path(p.strip()).expanduser() if p.strip() else Path.home()
        if not target.is_absolute(): target = _CWD / target
        target = target.resolve()
        if not target.exists(): return f"Not found: {target}"
        if not target.is_dir(): return f"Not a directory"
        _CWD = target; return f"cd -> {_CWD}"
    except Exception as e: return f"Error: {e}"

async def cmd_ls(p):
    try:
        path = Path(p.strip()).expanduser() if p.strip() else _CWD
        if not path.is_absolute(): path = _CWD / path
        path = path.resolve()
        if not path.exists(): return f"Not found: {path}"
        items = sorted(path.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))[:100]
        lines = [f"[ {path} ]"]
        for item in items:
            try:
                if item.is_dir(): lines.append(f"  [DIR ] {item.name}")
                else:
                    sz = item.stat().st_size
                    lines.append(f"  [FILE] {item.name}  ({mb(sz)})")
            except: lines.append(f"  [????] {item.name}")
        return "\n".join(lines)
    except Exception as e: return f"Error: {e}"

# 5. FILES
async def cmd_read_file(p):
    try:
        path = Path(p.strip()).expanduser()
        if not path.is_absolute(): path = _CWD / path
        if not path.exists(): return "Not found"
        try: sz = path.stat().st_size
        except PermissionError: return "Permission denied"
        if sz > 100_000: return "Too large (max 100 KB) — use download_file"
        try: return f"=== {path.name} ===\n{path.read_text(encoding='utf-8', errors='replace')[:4000]}"
        except PermissionError: return "Permission denied reading"
    except Exception as e: return f"Error: {e}"

async def cmd_write_file(p):
    try:
        if "|" not in p: return "Usage: write_file <path> | <content>"
        path_s, content = p.split("|", 1)
        path = Path(path_s.strip()).expanduser()
        if not path.is_absolute(): path = _CWD / path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content.lstrip("\n"), encoding="utf-8")
            return f"Written {len(content)} bytes to {path.name}"
        except PermissionError: return "Permission denied"
    except Exception as e: return f"Error: {e}"

async def cmd_download_file(p):
    try:
        path = Path(p.strip()).expanduser()
        if not path.is_absolute(): path = _CWD / path
        if not path.exists(): return "Not found"
        try: sz = path.stat().st_size
        except PermissionError: return "Permission denied"
        if sz > 20*1024*1024: return f"Too large ({mb(sz)}, max 20 MB)"
        try: data = path.read_bytes()
        except PermissionError: return "Cannot read — file may be locked"
        try:
            link = _upload(data, path.name)
            return f"Ready!\n{path.name} ({mb(sz)})\n{link}"
        except Exception as e: return f"Upload failed: {e}"
    except Exception as e: return f"Error: {e}"

async def cmd_create(p):
    if not p.strip() or "|" not in p: return "Usage: create name.txt | content"
    path_s, content = p.split("|", 1)
    path = Path(path_s.strip()).expanduser()
    if not path.is_absolute(): path = _CWD / path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.lstrip("\n"), encoding="utf-8")
        return f"Created: {path.name}"
    except Exception as e: return f"Error: {e}"

async def cmd_delete_file(p):
    try:
        path = Path(p.strip()).expanduser()
        if not path.is_absolute(): path = _CWD / path
        if not path.exists(): return "Not found"
        shutil.rmtree(path) if path.is_dir() else path.unlink()
        return f"Deleted: {path.name}"
    except PermissionError: return "Permission denied"
    except Exception as e: return f"Error: {e}"

async def cmd_copy_file(p):
    try:
        a, b = p.split("|", 1)
        src = Path(a.strip()).expanduser(); dst = Path(b.strip()).expanduser()
        if not src.is_absolute(): src = _CWD / src
        if not dst.is_absolute(): dst = _CWD / dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst); return f"Copied {src.name} -> {dst}"
    except Exception as e: return f"Error: {e}"

async def cmd_move_file(p):
    try:
        a, b = p.split("|", 1)
        src = Path(a.strip()).expanduser(); dst = Path(b.strip()).expanduser()
        if not src.is_absolute(): src = _CWD / src
        if not dst.is_absolute(): dst = _CWD / dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst)); return f"Moved {src.name} -> {dst}"
    except Exception as e: return f"Error: {e}"

async def cmd_mkdir(p):
    try:
        path = Path(p.strip()).expanduser()
        if not path.is_absolute(): path = _CWD / path
        path.mkdir(parents=True, exist_ok=True); return f"Created: {path}"
    except Exception as e: return f"Error: {e}"

async def cmd_find_file(p):
    try:
        parts = p.strip().split(None, 1)
        if not parts: return "Usage: find_file <pattern> [path]"
        pattern = parts[0]
        base = Path(parts[1]).expanduser() if len(parts) > 1 else _CWD
        results = [str(r) for r in base.rglob(pattern)][:40]
        return (f"Found {len(results)}:\n" + "\n".join(f"  {r}" for r in results)) if results else "No files found"
    except Exception as e: return f"Error: {e}"

async def cmd_recent_files(p):
    try:
        n = int(p.strip() or "10")
        files = []
        for ext in ("*.txt","*.pdf","*.docx","*.xlsx","*.png","*.jpg","*.mp4","*.zip"):
            files.extend(Path.home().rglob(ext))
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        lines = [f"Recent {n} files:"]
        for f in files[:n]:
            t = datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M")
            lines.append(f"  {t}  {f}")
        return "\n".join(lines) if len(lines) > 1 else "No files found"
    except Exception as e: return f"Error: {e}"

async def cmd_zip_folder(p):
    try:
        src = Path(p.strip()).expanduser()
        if not src.is_absolute(): src = _CWD / src
        if not src.exists(): return "Not found"
        out = shutil.make_archive(str(src.parent/src.name), "zip", str(src.parent), src.name)
        return f"Zipped: {Path(out).name}"
    except Exception as e: return f"Error: {e}"

async def cmd_run_script(p):
    try:
        path = Path(p.strip()).expanduser()
        if not path.is_absolute(): path = _CWD / path
        if not path.exists(): return "Not found"
        out = ps(str(path)) if path.suffix.lower() == ".ps1" else run(f'"{path}"')
        return f"Ran {path.name}:\n{out}"
    except Exception as e: return f"Error: {e}"

# 6. SHELL
async def cmd_shell(p):
    if not p.strip(): return "Usage: shell <command>"
    return f"$ {p[:60]}\n{run(p, cwd=_CWD)}"

async def cmd_powershell(p):
    if not IS_WIN: return "Windows only"
    if not p.strip(): return "Usage: powershell <command>"
    return f"PS> {p[:60]}\n{ps(p)}"

# 7. SYSTEM
async def cmd_status(p):
    lines = [
        f"=== {CFG.get('name','?')} ===",
        f"OS   : {platform.system()} {platform.release()} ({platform.machine()})",
        f"Host : {socket.gethostname()}",
        f"Time : {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"CWD  : {_CWD}",
    ]
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    up  = datetime.now() - datetime.fromtimestamp(psutil.boot_time())
    lines += [
        f"CPU  : {cpu}%",
        f"RAM  : {mb(mem.used)} / {mb(mem.total)} ({mem.percent}%)",
        f"Up   : {str(up).split('.')[0]}",
    ]
    try: lines.append(f"IP   : {socket.gethostbyname(socket.gethostname())}")
    except: pass
    return "\n".join(lines)

async def cmd_sysinfo(p):
    if IS_WIN: return run("systeminfo | findstr /C:\"OS\" /C:\"Processor\" /C:\"Memory\"")
    return run("uname -a")

async def cmd_cpu(p):
    total = psutil.cpu_percent(interval=1)
    cores = psutil.cpu_percent(percpu=True)
    lines = [f"CPU: {total}% | {psutil.cpu_count()} cores"]
    for i, c in enumerate(cores):
        bar = "█"*int(c/5) + "░"*(20-int(c/5))
        lines.append(f"  Core {i}: {bar} {c:.0f}%")
    return "\n".join(lines)

async def cmd_ram(p):
    m = psutil.virtual_memory()
    bar = "█"*int(m.percent/5) + "░"*(20-int(m.percent/5))
    return f"RAM: {bar} {m.percent}%\n  Used : {mb(m.used)}\n  Free : {mb(m.available)}\n  Total: {mb(m.total)}"

async def cmd_disk(p):
    lines = ["Disk:"]
    for part in psutil.disk_partitions():
        try:
            u = psutil.disk_usage(part.mountpoint)
            bar = "█"*int(u.percent/5) + "░"*(20-int(u.percent/5))
            lines.append(f"  {part.device}: {bar} {u.percent}%  {gb(u.used)}/{gb(u.total)}")
        except: pass
    return "\n".join(lines)

async def cmd_uptime(p):
    boot = datetime.fromtimestamp(psutil.boot_time())
    up   = datetime.now() - boot
    return f"Since: {boot:%Y-%m-%d %H:%M}\nUptime: {str(up).split('.')[0]}"

async def cmd_battery(p):
    try:
        b = psutil.sensors_battery()
        if not b: return "No battery"
        return f"Battery: {b.percent:.0f}% — {'Charging' if b.power_plugged else 'Discharging'}"
    except Exception as e: return f"Error: {e}"

async def cmd_temp(p):
    try:
        temps = psutil.sensors_temperatures()
        if not temps: return "No sensors"
        lines = []
        for name, entries in temps.items():
            for e in entries: lines.append(f"  {name}: {e.current:.0f}C")
        return "Temps:\n" + "\n".join(lines)
    except: return "Not supported"

async def cmd_processes(p):
    n = int(p.strip() or "15")
    procs = []
    for proc in psutil.process_iter(["pid","name","cpu_percent","memory_percent"]):
        try: procs.append(proc.info)
        except: pass
    procs.sort(key=lambda x: x.get("cpu_percent") or 0, reverse=True)
    lines = [f"{'PID':<8}{'CPU%':<7}{'MEM%':<7}Name"]
    for pr in procs[:n]:
        lines.append(f"{pr['pid']:<8}{pr['cpu_percent']:<7.1f}{pr['memory_percent']:<7.1f}{pr['name']}")
    return "\n".join(lines)

async def cmd_kill_process(p):
    p = p.strip()
    try:
        proc = psutil.Process(int(p)); name = proc.name(); proc.kill()
        return f"Killed {name} (PID {p})"
    except ValueError:
        killed = []
        for proc in psutil.process_iter(["pid","name"]):
            try:
                if p.lower() in proc.info["name"].lower():
                    proc.kill(); killed.append(proc.info["name"])
            except: pass
        return f"Killed: {', '.join(killed)}" if killed else f"Not found: {p}"
    except Exception as e: return f"Error: {e}"

async def cmd_whoami(p):
    return run("whoami")

async def cmd_env(p):
    key = p.strip()
    if key: return f"{key}={os.environ.get(key, 'Not set')}"
    return "\n".join(f"{k}={v}" for k,v in sorted(os.environ.items()))[:3000]

# 8. NETWORK
async def cmd_network(p):
    io_c = psutil.net_io_counters()
    lines = [f"Sent    : {mb(io_c.bytes_sent)}", f"Received: {mb(io_c.bytes_recv)}", "", "Interfaces:"]
    for iface, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family == socket.AF_INET:
                lines.append(f"  {iface}: {addr.address}")
    return "\n".join(lines)

async def cmd_connections(p):
    try:
        conns = psutil.net_connections(kind="inet")
        lines = [f"{'PID':<7}{'Status':<14}{'Local':<25}Remote"]
        for c in conns[:20]:
            la = f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else ""
            ra = f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else ""
            lines.append(f"{(c.pid or 0):<7}{c.status:<14}{la:<25}{ra}")
        return "\n".join(lines)
    except Exception as e: return f"Error: {e}"

async def cmd_public_ip(p):
    try:
        r = requests.get("https://api.ipify.org", timeout=10)
        return f"Public IP: {r.text.strip()}"
    except Exception as e: return f"Error: {e}"

async def cmd_ping_host(p):
    host = p.strip() or "8.8.8.8"
    flag = "-n" if IS_WIN else "-c"
    return f"Ping {host}:\n{run(f'ping {flag} 4 {host}')}"

async def cmd_traceroute(p):
    host = p.strip() or "google.com"
    return run(f"tracert {host}" if IS_WIN else f"traceroute {host}", timeout=60)

async def cmd_wifi_list(p):
    if IS_WIN: return run("netsh wlan show networks mode=bssid")
    return run("nmcli dev wifi list")

async def cmd_wifi_password(p):
    if not IS_WIN: return "Windows only"
    ssid = p.strip()
    if not ssid: return "Usage: wifi_password <SSID>"
    return ps(f'(netsh wlan show profile name="{ssid}" key=clear) -join "`n"')

# 9. CLIPBOARD
async def cmd_clipboard_get(p):
    if IS_WIN: return f"Clipboard:\n{ps('Get-Clipboard')}"
    return f"Clipboard:\n{pyperclip.paste()}"

async def cmd_clipboard_set(p):
    if not p.strip(): return "Usage: clipboard_set <text>"
    if IS_WIN:
        safe = p.replace("'", "\\'"); ps(f"Set-Clipboard -Value '{safe}'")
        return "Clipboard set"
    pyperclip.copy(p); return "Clipboard set"

async def cmd_clipboard_history(p):
    if IS_WIN: return ps("Get-Clipboard -Format Text -Raw")
    return "Windows only"

# 10. NOTIFICATIONS
async def cmd_notify(p):
    msg = p.strip() or "Hello"
    if IS_WIN:
        script = f'Add-Type -AssemblyName System.Windows.Forms;$n=New-Object System.Windows.Forms.NotifyIcon;$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;$n.ShowBalloonTip(5000,"RC","{msg}",[System.Windows.Forms.ToolTipIcon]::None);Start-Sleep 6;$n.Dispose()'
        subprocess.Popen(["powershell","-NoProfile","-WindowStyle","Hidden","-Command",script])
    elif IS_MAC: run(f'osascript -e \'display notification "{msg}" with title "RC"\'')
    else: run(f'notify-send "RC" "{msg}"')
    return "Notified"

async def cmd_msgbox(p):
    if not IS_WIN: return "Windows only"
    msg = p.strip() or "Hello"
    ps(f'Add-Type -AssemblyName System.Windows.Forms;[System.Windows.Forms.MessageBox]::Show("{msg}","RC")')
    return "Message shown"

async def cmd_speak(p):
    if not IS_WIN: return "Windows only"
    text = p.strip() or "Hello"
    ps(f'Add-Type -AssemblyName System.Speech;(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak("{text}")')
    return f"Speaking: {text}"

async def cmd_play_sound(p):
    path = p.strip()
    if IS_WIN: ps(f'(New-Object Media.SoundPlayer "{path}").PlaySync()')
    elif IS_MAC: run(f'afplay "{path}"')
    else: run(f'aplay "{path}"')
    return "Playing"

async def cmd_volume(p):
    if not IS_WIN: return "Windows only"
    try:
        level = int(p.strip())
        ps(f'$obj=new-object -com wscript.shell;1..50|%{{$obj.SendKeys([char]174)}};1..{level//2}|%{{$obj.SendKeys([char]175)}}')
        return f"Volume ~{level}%"
    except: return "Usage: volume 0-100"

# 11. POWER
async def cmd_shutdown(p):
    try: d = int(p.strip() or "30")
    except: d = 30
    run(f"shutdown /s /t {d}" if IS_WIN else f"shutdown -h +{max(1,d//60)}")
    return f"Shutdown in {d}s"

async def cmd_restart(p):
    try: d = int(p.strip() or "30")
    except: d = 30
    run(f"shutdown /r /t {d}" if IS_WIN else f"shutdown -r +{max(1,d//60)}")
    return f"Restart in {d}s"

async def cmd_cancel_shutdown(p):
    return run("shutdown /a" if IS_WIN else "shutdown -c")

async def cmd_sleep(p):
    run("rundll32.exe powrprof.dll,SetSuspendState 0,1,0" if IS_WIN else "systemctl suspend")
    return "Sleeping..."

async def cmd_lock(p):
    run("rundll32.exe user32.dll,LockWorkStation" if IS_WIN else "loginctl lock-session")
    return "Locked"

# 12. APPS
async def cmd_open_app(p):
    try:
        if IS_WIN: os.startfile(p.strip())
        elif IS_MAC: subprocess.Popen(["open", p.strip()])
        else: subprocess.Popen(["xdg-open", p.strip()])
        return f"Opened: {p.strip()}"
    except Exception as e: return f"Error: {e}"

async def cmd_open_url(p):
    import webbrowser
    try: webbrowser.open(p.strip()); return f"Opened: {p.strip()}"
    except Exception as e: return f"Error: {e}"

async def cmd_close_app(p):
    if not p.strip(): return "Usage: close_app <name>"
    if IS_WIN: return run(f'taskkill /IM "{p.strip()}" /F')
    return run(f"pkill -f {p.strip()}")

async def cmd_list_windows(p):
    if not IS_WIN: return "Windows only"
    return ps("Get-Process | Where-Object {$_.MainWindowTitle} | Select-Object Id,ProcessName,MainWindowTitle | Format-Table -AutoSize | Out-String")

async def cmd_show_desktop(p):
    if IS_WIN: run("explorer.exe shell:::{3080F90D-D7AD-11D9-BD98-0000947B0257}")
    return "Desktop shown"

async def cmd_task_manager(p):
    if IS_WIN: subprocess.Popen(["taskmgr.exe"])
    return "Task manager opened"

async def cmd_close_all_apps(p):
    if not IS_WIN: return "Windows only"
    return ps("Get-Process | Where-Object {$_.MainWindowHandle -ne 0} | ForEach-Object {$_.CloseMainWindow()}")

async def cmd_schedule_task(p):
    if not IS_WIN: return "Windows only"
    try:
        parts = p.split("|")
        if len(parts) < 2: return "Usage: schedule_task HH:MM | command"
        t = parts[0].strip(); cmd_s = parts[1].strip()
        return run(f'schtasks /create /tn "RC_Task" /tr "{cmd_s}" /sc once /st {t} /f')
    except Exception as e: return f"Error: {e}"

# 13. CONTROL
async def cmd_disable_input(p):
    if not IS_WIN: return "Windows only"
    try:
        secs = min(int(p.strip() or "5"), 30)
        import ctypes
        ctypes.windll.user32.BlockInput(True)
        try: await asyncio.sleep(secs)
        finally: ctypes.windll.user32.BlockInput(False)
        return f"Input blocked for {secs}s"
    except Exception as e:
        try: import ctypes; ctypes.windll.user32.BlockInput(False)
        except: pass
        return f"Error: {e}"

async def cmd_set_wallpaper(p):
    if not IS_WIN: return "Windows only"
    path = p.strip()
    if not Path(path).exists(): return "File not found"
    try:
        import ctypes; ctypes.windll.user32.SystemParametersInfoW(20, 0, path, 3)
        return "Wallpaper set"
    except Exception as e: return f"Error: {e}"

async def cmd_set_resolution(p):
    if not IS_WIN: return "Windows only"
    try:
        w, h = map(int, p.split())
        script = f"""Add-Type @"
using System.Runtime.InteropServices;
public class D{{[DllImport("user32.dll")]public static extern bool ChangeDisplaySettings(ref M dm,int f);public struct M{{public int W;public int H;}}}}
"@
$m=New-Object D+M;$m.W={w};$m.H={h};[D]::ChangeDisplaySettings([ref]$m,0)"""
        ps(script); return f"Resolution set to {w}x{h}"
    except: return "Usage: set_resolution width height"

# 14. ADVANCED
async def cmd_registry_read(p):
    if not IS_WIN: return "Windows only"
    if not p.strip(): return "Usage: registry_read HKCU\\..."
    return run(f'reg query "{p.strip()}"')

async def cmd_get_windows_key(p):
    if not IS_WIN: return "Windows only"
    out = ps(r"(Get-WmiObject -query 'select * from SoftwareLicensingService').OA3xOriginalProductKey")
    if not out.strip():
        out = ps(r"(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SoftwareProtectionPlatform' -Name BackupProductKeyDefault).BackupProductKeyDefault")
    return f"Windows Key: {out.strip()}" if out.strip() else "Could not retrieve key"

async def cmd_empty_recycle(p):
    if not IS_WIN: return "Windows only"
    ps("Clear-RecycleBin -Force -ErrorAction SilentlyContinue")
    return "Recycle bin emptied"

# 15. STARTUP
async def cmd_add_startup(p):
    if not IS_WIN: return "Windows only"
    try:
        exe     = Path(sys.executable).resolve()
        workdir = exe.parent
        startup = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/RemoteControl.lnk"
        ps1 = Path(os.environ.get("TEMP","C:\\Temp")) / "rc_add_startup.ps1"
        ps1.write_text(
            f'$WS=New-Object -ComObject WScript.Shell\n'
            f'$SC=$WS.CreateShortcut("{startup}")\n'
            f'$SC.TargetPath="{exe}"\n'
            f'$SC.WorkingDirectory="{workdir}"\n'
            f'$SC.WindowStyle=7\n'
            f'$SC.Save()\n',
            encoding="utf-8"
        )
        result = run(f'powershell -NoProfile -ExecutionPolicy Bypass -File "{ps1}"')
        ps1.unlink(missing_ok=True)
        return f"Added to startup:\n  {startup}\n  Target: {exe}"
    except Exception as e: return f"Error: {e}"

async def cmd_remove_startup(p):
    if not IS_WIN: return "Windows only"
    try:
        lnk = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/RemoteControl.lnk"
        if lnk.exists(): lnk.unlink(); return "Removed from startup"
        return run(r'reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "RemoteControl" /f')
    except Exception as e: return f"Error: {e}"

# 16. PRANKS
async def cmd_prank_update(p):
    if not IS_WIN: return "Windows only"
    msg = p.strip() or "Configuring Windows updates 0%..."
    script = f'Add-Type -AssemblyName System.Windows.Forms;[System.Windows.Forms.MessageBox]::Show("{msg}","Windows Update",[System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Information)'
    subprocess.Popen(["powershell","-NoProfile","-WindowStyle","Hidden","-Command",script])
    return "Prank sent"

async def cmd_prank_bsod(p):
    if not IS_WIN: return "Windows only"
    script = r"""Add-Type -TypeDefinition @"
using System;using System.Runtime.InteropServices;
public class B{[DllImport("ntdll.dll")]public static extern uint RtlAdjustPrivilege(int p,bool e,bool t,ref bool prev);[DllImport("ntdll.dll")]public static extern uint NtRaiseHardError(uint ec,uint n,uint u,IntPtr p,uint f,ref uint r);public static void Go(){bool prev=false;uint r=0;RtlAdjustPrivilege(19,true,false,ref prev);NtRaiseHardError(0xc0000022,0,0,IntPtr.Zero,6,ref r);}}
"@
[B]::Go()"""
    subprocess.Popen(["powershell","-NoProfile","-WindowStyle","Hidden","-Command",script])
    return "BSOD initiated"

# 17. CONFIG
async def cmd_show_config(p):
    safe = {k:("***" if "key" in k or "secret" in k else v) for k,v in CFG.items()}
    return "\n".join(f"{k}: {v}" for k,v in safe.items())

async def cmd_update_server(p):
    url = p.strip()
    if not url: return "Usage: update_server https://..."
    CFG["server_url"] = url; save_config(CFG)
    return f"Server URL updated -> {url}\nRestart to reconnect"

async def cmd_update_name(p):
    name = p.strip()
    if not name: return "Usage: update_name <name>"
    CFG["name"] = name; save_config(CFG); return f"Name updated -> {name}"

async def cmd_update_secret(p):
    key = p.strip()
    if not key: return "Usage: update_secret <key>"
    CFG["secret_key"] = key; save_config(CFG); return "Secret updated"

# 18. UPDATE
async def cmd_voice_mics(p):
  """Список микрофонов — укажите индекс в config.json -> voice.mic_index."""
  lines = ["Доступные микрофоны:"]
  for i, name in enumerate(sr.Microphone.list_microphone_names()):
    mark = " <-- active" if voice_engine and voice_engine.cfg.get("mic_index") == i else ""
    lines.append(f"  [{i}] {name}{mark}")
  return "\n".join(lines)

async def cmd_voice_test(p):
  """Проверка голоса — слушает и возвращает распознанный текст."""
  if not voice_engine:
    return "Voice engine not ready"
  try:
    heard = await asyncio.to_thread(voice_engine.listen)
    return f"Распознано: {heard}" if heard else "Ничего не распознано"
  except sr.WaitTimeoutError:
    return "Таймаут — ничего не сказано"
  except sr.UnknownValueError:
    return "Речь не распознана — повторите"
  except Exception as e:
    return f"Error: {e}"

async def cmd_version(p):
    return f"Client version: v{CLIENT_VERSION}"

async def cmd_check_update(p):
    url = CFG.get("update_url","").strip()
    if not url: return "No update_url in config.json"
    ver, dl, sha, sig = await check_for_update()
    if not ver: return f"Already latest (v{CLIENT_VERSION}) or update metadata rejected (check sha256+sig in version.json)"
    try:
        r = requests.get(dl, timeout=60)
        if r.status_code != 200:
            return f"Download failed: HTTP {r.status_code}"
        if not _verify_sha256(r.content, sha):
            return (f"Update v{ver} REJECTED: sha256 mismatch "
                    f"(metadata: {sha} — check version.json)")
        if dl.endswith(".zip"):
            _UPDATE_ZIP.write_bytes(r.content)
        else:
            _UPDATE_STAGED.write_bytes(r.content)
        _UPDATE_VER_F.write_text(str(ver))
        return f"Update v{ver} downloaded, verified (sha256 + Ed25519). Send: restart_update to apply"
    except Exception as e: return f"Download error: {e}"

async def cmd_restart_update(p):
    if not (_UPDATE_STAGED.exists() or _UPDATE_ZIP.exists()): return "No pending update. Send: check_update first"
    if apply_pending_update():
        _time.sleep(0.5); os._exit(0)
    return "Could not apply update"

# 19. UNINSTALL
async def cmd_uninstall(p):
    exe_path = Path(sys.executable)
    app_dir  = exe_path.parent
    log_dir  = _LOG_DIR
    startup_lnk = Path(os.environ.get("APPDATA","")) / "Microsoft/Windows/Start Menu/Programs/Startup/RemoteControl.lnk"
    removed = []
    try:
        if startup_lnk.exists(): startup_lnk.unlink(); removed.append(f"Shortcut: {startup_lnk}")
    except Exception as e: removed.append(f"Shortcut error: {e}")
    try:
        run(r'reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "RemoteControl" /f')
        removed.append("Registry entry removed")
    except: pass
    try:
        if log_dir.exists(): shutil.rmtree(log_dir); removed.append(f"Logs: {log_dir}")
    except Exception as e: removed.append(f"Logs error: {e}")
    try:
        cfg = app_dir / "config.json"
        if cfg.exists(): cfg.unlink(); removed.append(f"Config: {cfg}")
    except Exception as e: removed.append(f"Config error: {e}")

    result = "Uninstall complete:\n" + "\n".join(removed) + "\n\nDeleting exe in 3s..."

    if IS_WIN and getattr(sys, "frozen", False):
        bat_lines = [
            "@echo off", "timeout /t 3 /nobreak >nul",
            f'del /f /q "{exe_path}"',
            f'rmdir /s /q "{app_dir}"',
            'del /f /q "%~f0"',
        ]
        bat = Path(os.environ.get("TEMP","C:\\Temp")) / "rc_uninstall.bat"
        bat.write_text("\n".join(bat_lines), encoding="utf-8")
        subprocess.Popen(f'start "" /b cmd /c "{bat}"', shell=True,
                         creationflags=subprocess.CREATE_NO_WINDOW)

    def _exit(): _time.sleep(1); os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()
    return result

# 20. MISC
async def cmd_hello(p):
    return f"Hello from {CFG.get('name','?')} — online! (v{CLIENT_VERSION}, {len(COMMANDS)} commands)"

async def cmd_my_task(p):
    return f"Custom task: {p.strip() or '(no params)'}"

async def cmd_help(p):
    return f"""Remote Control v{CLIENT_VERSION} — {len(COMMANDS)} commands

SCREENSHOT  : screenshot, screenshot_region, screen_size
MOUSE       : mouse_move, mouse_click, mouse_scroll, mouse_pos, mouse_drag
KEYBOARD    : type_text, hotkey, press_key
NAVIGATION  : pwd, cd, ls (also: dir, sd)
FILES       : read_file, write_file, download_file, create, delete_file,
              copy_file, move_file, mkdir, find_file, zip_folder,
              recent_files, run_script
SHELL       : shell, powershell
SYSTEM      : status, sysinfo, cpu, ram, disk, uptime, battery, temp,
              processes, kill_process, whoami, env
NETWORK     : network, connections, public_ip, ping_host, traceroute,
              wifi_list, wifi_password
CLIPBOARD   : clipboard_get, clipboard_set, clipboard_history
NOTIFY      : notify, msgbox, speak, play_sound, volume
POWER       : shutdown, restart, cancel_shutdown, sleep, lock
APPS        : open_app, open_url, close_app, list_windows, show_desktop,
              task_manager, close_all_apps, schedule_task
CONTROL     : disable_input, set_wallpaper, set_resolution
ADVANCED    : registry_read, get_windows_key, empty_recycle
STARTUP     : add_startup, remove_startup
PRANKS      : prank_update, prank_bsod
CONFIG      : show_config, update_server, update_name, update_secret
UPDATE      : version, check_update, restart_update
VOICE       : voice_mics, voice_test  (настройки в config.json -> voice)
OTHER       : uninstall, hello, my_task, help

CHAINING    : /send pc screenshot /and lock /and notify Done"""



# ─────────────────────────────────────────────────────────────────────────────
#  COMMAND ROUTER
# ─────────────────────────────────────────────────────────────────────────────
COMMANDS = {
    "screenshot":        cmd_screenshot,
    "screenshot_region": cmd_screenshot_region,
    "screen_size":       cmd_screen_size,
    "mouse_move":        cmd_mouse_move,
    "mouse_click":       cmd_mouse_click,
    "mouse_scroll":      cmd_mouse_scroll,
    "mouse_pos":         cmd_mouse_pos,
    "mouse_drag":        cmd_mouse_drag,
    "type_text":         cmd_type_text,
    "hotkey":            cmd_hotkey,
    "press_key":         cmd_press_key,
    "pwd":               cmd_pwd,
    "cd":                cmd_cd,
    "ls":                cmd_ls,
    "dir":               cmd_ls,
    "sd":                cmd_ls,
    "read_file":         cmd_read_file,
    "write_file":        cmd_write_file,
    "download_file":     cmd_download_file,
    "create":            cmd_create,
    "delete_file":       cmd_delete_file,
    "copy_file":         cmd_copy_file,
    "move_file":         cmd_move_file,
    "mkdir":             cmd_mkdir,
    "find_file":         cmd_find_file,
    "recent_files":      cmd_recent_files,
    "zip_folder":        cmd_zip_folder,
    "run_script":        cmd_run_script,
    "shell":             cmd_shell,
    "powershell":        cmd_powershell,
    "status":            cmd_status,
    "sysinfo":           cmd_sysinfo,
    "cpu":               cmd_cpu,
    "ram":               cmd_ram,
    "disk":              cmd_disk,
    "uptime":            cmd_uptime,
    "battery":           cmd_battery,
    "temp":              cmd_temp,
    "processes":         cmd_processes,
    "kill_process":      cmd_kill_process,
    "whoami":            cmd_whoami,
    "env":               cmd_env,
    "network":           cmd_network,
    "connections":       cmd_connections,
    "public_ip":         cmd_public_ip,
    "ping_host":         cmd_ping_host,
    "traceroute":        cmd_traceroute,
    "wifi_list":         cmd_wifi_list,
    "wifi_password":     cmd_wifi_password,
    "clipboard_get":     cmd_clipboard_get,
    "clipboard_set":     cmd_clipboard_set,
    "clipboard_history": cmd_clipboard_history,
    "notify":            cmd_notify,
    "msgbox":            cmd_msgbox,
    "speak":             cmd_speak,
    "play_sound":        cmd_play_sound,
    "volume":            cmd_volume,
    "shutdown":          cmd_shutdown,
    "restart":           cmd_restart,
    "cancel_shutdown":   cmd_cancel_shutdown,
    "sleep":             cmd_sleep,
    "lock":              cmd_lock,
    "open_app":          cmd_open_app,
    "open_url":          cmd_open_url,
    "close_app":         cmd_close_app,
    "list_windows":      cmd_list_windows,
    "show_desktop":      cmd_show_desktop,
    "task_manager":      cmd_task_manager,
    "close_all_apps":    cmd_close_all_apps,
    "schedule_task":     cmd_schedule_task,
    "disable_input":     cmd_disable_input,
    "set_wallpaper":     cmd_set_wallpaper,
    "set_resolution":    cmd_set_resolution,
    "registry_read":     cmd_registry_read,
    "get_windows_key":   cmd_get_windows_key,
    "empty_recycle":     cmd_empty_recycle,
    "add_startup":       cmd_add_startup,
    "remove_startup":    cmd_remove_startup,
    "prank_update":      cmd_prank_update,
    "prank_bsod":        cmd_prank_bsod,
    "show_config":       cmd_show_config,
    "update_server":     cmd_update_server,
    "update_name":       cmd_update_name,
    "update_secret":     cmd_update_secret,
    "version":           cmd_version,
    "voice_mics":        cmd_voice_mics,
    "voice_test":        cmd_voice_test,
    "check_update":      cmd_check_update,
    "restart_update":    cmd_restart_update,
    "uninstall":         cmd_uninstall,
    "hello":             cmd_hello,
    "my_task":           cmd_my_task,
    "help":              cmd_help,
}

# ── Plugin commands (зарегистрированы при загрузке плагинов) ─────────────────
async def cmd_plugins_list(p: str) -> str:
    if not _PLUGINS_AVAILABLE or not plugin_manager:
        return "Plugin system not available."
    plugins = plugin_manager.list_plugins()
    if not plugins:
        return "No plugins loaded. Put .py files in the plugins/ folder."
    lines = [f"Plugins ({len(plugins)}):"]
    for pl in plugins:
        lines.append(f"\n  {pl['name']} v{pl['version']} — {pl['description']}")
        lines.append(f"  Commands: {', '.join(pl['commands'])}")
        if pl['voice_triggers']:
            lines.append(f"  Voice triggers: {pl['voice_triggers']}")
    return "\n".join(lines)

async def cmd_plugin_install(p: str) -> str:
    if not _PLUGINS_AVAILABLE or not plugin_manager:
        return "Plugin system not available."
    parts = (p or "").split()
    if len(parts) != 2:
        return ("Usage: plugin_install <url> <sha256>\n"
                "sha256 is REQUIRED. Get it from the plugin author, verify the file "
                "yourself first (certutil -hashfile plugin.py SHA256).")
    url, sha = parts[0], parts[1]
    return await plugin_manager.install(url, sha)

async def cmd_plugin_unload(p: str) -> str:
    if not _PLUGINS_AVAILABLE or not plugin_manager:
        return "Plugin system not available."
    if not p:
        return "Usage: plugin_unload <name>"
    return await plugin_manager.unload(p)

async def cmd_plugin_delete(p: str) -> str:
    if not _PLUGINS_AVAILABLE or not plugin_manager:
        return "Plugin system not available."
    if not p:
        return "Usage: plugin_delete <name>"
    return await plugin_manager.delete(p)

COMMANDS["plugins_list"]    = cmd_plugins_list
COMMANDS["plugin_install"]  = cmd_plugin_install
COMMANDS["plugin_unload"]   = cmd_plugin_unload
COMMANDS["plugin_delete"]   = cmd_plugin_delete

# ─────────────────────────────────────────────────────────────────────────────
#  /and CHAINING
# ─────────────────────────────────────────────────────────────────────────────
async def handle_chained(cmd_str: str) -> str:
    parts = cmd_str.split(" /and ")
    results = []
    for i, part in enumerate(parts, 1):
        part = part.strip()
        if not part: continue
        result = await dispatch(part)
        if result.startswith("Error") or "denied" in result.lower():
            results.append(f"[{i}] FAIL: {part}\n  {result}")
            break
        results.append(f"[{i}] OK: {part}")
    return "\n".join(results)

async def dispatch(cmd: str) -> str:
    if " /and " in cmd:
        return await handle_chained(cmd)
    parts = cmd.strip().split(None, 1)
    name  = parts[0].lower() if parts else ""
    param = parts[1] if len(parts) > 1 else ""
    if name in COMMANDS:
        try:
            return str(await COMMANDS[name](param))
        except Exception as e:
            log.exception(f"Command '{name}' crashed")
            return f"Error in {name}: {e}"
    # Попробовать плагины
    if _PLUGINS_AVAILABLE and plugin_manager and plugin_manager.is_plugin_cmd(name):
        try:
            return str(await plugin_manager.dispatch(name, param))
        except Exception as e:
            return f"Plugin error in {name}: {e}"
    return f"Unknown: {name!r} — type 'help' to see all {len(COMMANDS)} commands"

# ─────────────────────────────────────────────────────────────────────────────
#  WEBSOCKET LOOP — THE CORE CONNECTION
# ─────────────────────────────────────────────────────────────────────────────
async def ws_loop(stop_evt: threading.Event):
    """
    Connect to server, register, listen for commands.
    Reconnects automatically with exponential backoff.
    Sends WS-level ping every 25s + app-level ping every 20s.
    """
    global _ws_connected
    fails = 0

    while not stop_evt.is_set():
        try:
            url = normalize_ws_url(CFG["server_url"])
            log.info(f"Connecting to {url} ...")

            async with websockets.connect(
                url,
                open_timeout=30,    # Render может медленно делать WS handshake
                ping_interval=25,
                ping_timeout=20,
                close_timeout=5,
            ) as ws:

                # ── REGISTER ─────────────────────────────────────────────
                plugin_info = plugin_manager.list_plugins() if (_PLUGINS_AVAILABLE and plugin_manager) else []
                await ws.send(json.dumps({
                    "type":    "register",
                    "name":    CFG["name"],
                    "secret":  CFG["secret_key"],
                    "plugins": plugin_info,
                }))

                resp = json.loads(await ws.recv())
                if resp.get("type") == "error":
                    log.error(f"Rejected by server: {resp.get('msg')}")
                    _ws_connected = False
                    await asyncio.sleep(5)
                    fails += 1
                    continue

                fails         = 0
                _ws_connected = True
                log.info(f"Registered as '{CFG['name']}' — {len(COMMANDS)} commands ready")

                # ── APP-LEVEL HEARTBEAT ───────────────────────────────────
                async def heartbeat():
                    while not stop_evt.is_set():
                        await asyncio.sleep(20)
                        try:
                            await ws.send(json.dumps({"type": "ping"}))
                        except Exception:
                            break

                # ── AUTO-UPDATE CHECKER ───────────────────────────────────
                hb  = asyncio.create_task(heartbeat())
                upd = asyncio.create_task(update_checker_loop())

                try:
                    async for raw in ws:
                        if stop_evt.is_set():
                            break
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue

                        if msg.get("type") == "command":
                            cmd = msg.get("command", "")
                            log.info(f"<- {cmd[:80]}")
                            result = await dispatch(cmd)
                            log.info(f"-> {result[:80]}")
                            reply = {
                                "type":    "result",
                                "command": cmd,
                                "result":  result,
                            }
                            if "reply_chat_id" in msg:
                                reply["reply_chat_id"] = msg["reply_chat_id"]
                            try:
                                await ws.send(json.dumps(reply))
                            except Exception:
                                pass
                finally:
                    _ws_connected = False
                    hb.cancel()
                    upd.cancel()

        except Exception as e:
            _ws_connected = False
            fails += 1
            wait = min(5 * fails, 60)
            log.warning(f"Disconnected: {e} — retry in {wait}s")
            await asyncio.sleep(wait)


# =============================================================================
#  IPC-СЕРВЕР — позволяет отдельному GUI-процессу подключаться/отключаться,
#  не затрагивая основной воркер (фикс бага "закрыл GUI — умер весь апп")
# =============================================================================
IPC_HOST = "127.0.0.1"
IPC_PORT = 47231
_ipc_clients = set()

async def _ipc_handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info("peername")
    try:
        auth_line = await asyncio.wait_for(reader.readline(), timeout=5)
        auth = json.loads(auth_line.decode("utf-8"))
        if auth.get("key") != CFG.get("secret_key"):
            log.warning(f"IPC auth failed from {peer}")
            writer.close()
            return
        log.info(f"GUI attached ({peer})")
        _ipc_clients.add(writer)
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            if msg.get("type") == "cmd":
                cmd = msg.get("cmd", "")
                result = await dispatch(cmd)
                out = json.dumps({"type": "result", "cmd": cmd, "result": result}) + "\n"
                writer.write(out.encode("utf-8"))
                await writer.drain()
            elif msg.get("type") == "stats":
                out = json.dumps({"type": "stats", "connected": _ws_connected}) + "\n"
                writer.write(out.encode("utf-8"))
                await writer.drain()
    except (asyncio.TimeoutError, ConnectionResetError, json.JSONDecodeError):
        pass
    except Exception as e:
        log.debug(f"IPC error: {e}")
    finally:
        _ipc_clients.discard(writer)
        try:
            writer.close()
        except Exception:
            pass
        log.info(f"GUI detached ({peer})")

async def ipc_server_loop():
    """Runs forever inside the headless worker. GUI processes attach/detach freely."""
    global IPC_PORT
    for port in range(IPC_PORT, IPC_PORT + 10):
        try:
            srv = await asyncio.start_server(_ipc_handle, IPC_HOST, port)
            IPC_PORT = port
            log.info(f"IPC server on port {port}")
            async with srv:
                await srv.serve_forever()
            return
        except OSError:
            log.debug(f"IPC port {port} busy, trying next...")
    log.error("IPC server: no free port found in range 47231-47240")


# =============================================================================
#  ГОЛОС — SpeechRecognition + pyttsx3 (настройки в config.json -> voice)
# =============================================================================
voice_engine = None

class VoiceEngine:
    """Распознавание и синтез речи с настройками из config.json."""

    def __init__(self, cfg: dict):
        self.cfg = {**DEFAULT_CFG["voice"], **cfg.get("voice", {})}
        self._rec = sr.Recognizer()
        self._rec.dynamic_energy_threshold = bool(self.cfg.get("dynamic_energy", True))
        self._rec.pause_threshold = float(self.cfg.get("pause_threshold", 0.8))
        thr = self.cfg.get("energy_threshold")
        if thr is not None:
            self._rec.energy_threshold = float(thr)
        self._lang = self.cfg.get("language", "en-US")
        self._timeout = int(self.cfg.get("timeout", 8))
        self._phrase_limit = int(self.cfg.get("phrase_time_limit", 12))
        self._ambient = float(self.cfg.get("ambient_duration", 1.0))
        self._mic_index = self.cfg.get("mic_index")
        self._tts_rate = int(self.cfg.get("tts_rate", 165))
        self._tts_voice_id = self.cfg.get("tts_voice_id")
        self._tts = None
        self._ready = False

    def _mic(self):
        if self._mic_index is not None:
            return sr.Microphone(device_index=int(self._mic_index))
        return sr.Microphone()

    def initialize(self) -> str:
        """Калибровка микрофона при старте."""
        try:
            with self._mic() as source:
                self._rec.adjust_for_ambient_noise(source, duration=self._ambient)
            self._ready = True
            mics = sr.Microphone.list_microphone_names()
            idx = self._mic_index if self._mic_index is not None else "default"
            return f"Voice ready (lang={self._lang}, mic={idx}, devices={len(mics)})"
        except Exception as e:
            log.error(f"Voice init failed: {e}")
            return f"Voice init error: {e}"

    def listen(self) -> str:
        with self._mic() as source:
            if not self._ready:
                self._rec.adjust_for_ambient_noise(source, duration=0.5)
            audio = self._rec.listen(
                source,
                timeout=self._timeout,
                phrase_time_limit=self._phrase_limit,
            )
        return self._rec.recognize_google(audio, language=self._lang)

    def speak(self, text: str) -> None:
        def _go():
            try:
                if self._tts is None:
                    self._tts = pyttsx3.init()
                    self._tts.setProperty("rate", self._tts_rate)
                    if self._tts_voice_id:
                        self._tts.setProperty("voice", self._tts_voice_id)
                    elif self._lang.startswith("ru"):
                        for v in self._tts.getProperty("voices"):
                            if "ru" in v.id.lower() or "russian" in v.name.lower():
                                self._tts.setProperty("voice", v.id)
                                break
                self._tts.say(str(text)[:300])
                self._tts.runAndWait()
            except Exception as e:
                log.error(f"TTS error: {e}")
        threading.Thread(target=_go, daemon=True).start()

def _speak(text: str):
    if voice_engine:
        voice_engine.speak(text)

def _listen_once(timeout: int = None) -> str:
    if not voice_engine:
        return ""
    try:
        return voice_engine.listen()
    except sr.WaitTimeoutError:
        return ""
    except sr.UnknownValueError:
        return ""
    except Exception as e:
        log.warning(f"Voice listen error: {e}")
        return ""

# ─────────────────────────────────────────────────────────────────────────────
#  FLOATING GUI PANEL
# ─────────────────────────────────────────────────────────────────────────────
class FloatingPanel:
    W, H, PAD = 310, 400, 10
    BG      = "#0d1117"
    SURFACE = "#161b22"
    SURF2   = "#1c2128"
    BORDER  = "#30363d"
    TEXT    = "#e6edf3"
    MUTED   = "#8b949e"
    BLUE    = "#58a6ff"
    GREEN   = "#3fb950"
    AMBER   = "#d29922"
    RED     = "#f85149"
    PURPLE  = "#a371f7"

    def __init__(self, submit_fn, result_queue: "_queue.Queue"):
        self._submit    = submit_fn
        self._rq        = result_queue
        self._listening = False
        self._collapsed = False

        self.root = tk.Tk()
        self.root.title("RC")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.95)
        self.root.configure(bg=self.BG)
        self._set_icon()

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x  = sw - self.W - self.PAD
        y  = sh - self.H - self.PAD - 48
        self.root.geometry(f"{self.W}x{self.H}+{x}+{y}")

        self._build_ui()
        self._make_draggable()
        self.root.after(200, self._poll_results)
        self.root.after(100, self._update_stats)

    def _set_icon(self):
        for candidate in (APP_DIR / "icon.ico", APP_DIR / "assets" / "icon.ico"):
            if candidate.exists():
                try:
                    self.root.iconbitmap(str(candidate))
                    return
                except Exception:
                    pass

    def _build_ui(self):
        tbar = tk.Frame(self.root, bg=self.SURFACE, height=30)
        tbar.pack(fill="x"); tbar.pack_propagate(False)
        tk.Label(tbar, text="  Remote Control", bg=self.SURFACE, fg=self.TEXT,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(side="left")
        tk.Button(tbar, text="x", bg=self.SURFACE, fg=self.RED,
                  font=("Segoe UI", 9, "bold"), relief="flat",
                  activebackground=self.SURF2, cursor="hand2",
                  command=self.root.destroy, width=3).pack(side="right")
        self._btn_col = tk.Button(tbar, text="-", bg=self.SURFACE, fg=self.MUTED,
                  font=("Segoe UI", 10, "bold"), relief="flat",
                  activebackground=self.SURF2, cursor="hand2",
                  command=self._toggle_collapse, width=3)
        self._btn_col.pack(side="right")

        self._body = tk.Frame(self.root, bg=self.BG)
        self._body.pack(fill="both", expand=True)

        tbar2 = tk.Frame(self._body, bg=self.SURFACE)
        tbar2.pack(fill="x")
        self._tab_btns = {}
        for name, lbl in [("stats","Stats"),("term","Terminal"),("voice","Voice"),("tools","Tools")]:
            b = tk.Button(tbar2, text=lbl, bg=self.SURFACE, fg=self.MUTED,
                          font=("Segoe UI", 8), relief="flat",
                          activebackground=self.SURF2, cursor="hand2",
                          padx=6, pady=3,
                          command=lambda n=name: self._show_tab(n))
            b.pack(side="left")
            self._tab_btns[name] = b

        self._content = tk.Frame(self._body, bg=self.BG)
        self._content.pack(fill="both", expand=True, padx=6, pady=4)
        self._frames = {}
        self._build_stats()
        self._build_terminal()
        self._build_voice()
        self._build_tools()
        self._show_tab("stats")

    def _surf_row(self, parent, label, var, color):
        row = tk.Frame(parent, bg=self.SURF2)
        row.pack(fill="x", pady=1)
        tk.Label(row, text=label, bg=self.SURF2, fg=self.MUTED,
                 font=("Consolas", 8), width=7, anchor="w").pack(side="left", padx=6)
        tk.Label(row, textvariable=var, bg=self.SURF2, fg=color,
                 font=("Consolas", 8, "bold"), anchor="w").pack(side="left", padx=4)

    def _build_stats(self):
        f = tk.Frame(self._content, bg=self.BG)
        self._frames["stats"] = f
        self._v_conn = tk.StringVar(value="Connecting...")
        self._v_cpu  = tk.StringVar(value="—")
        self._v_ram  = tk.StringVar(value="—")
        self._v_disk = tk.StringVar(value="—")
        self._v_bat  = tk.StringVar(value="—")
        self._v_up   = tk.StringVar(value="—")
        self._v_time = tk.StringVar(value="—")

        row = tk.Frame(f, bg=self.SURF2)
        row.pack(fill="x", pady=1)
        tk.Label(row, text="Server", bg=self.SURF2, fg=self.MUTED,
                 font=("Consolas", 8), width=7, anchor="w").pack(side="left", padx=6)
        self._lbl_conn = tk.Label(row, textvariable=self._v_conn, bg=self.SURF2,
                                   fg=self.AMBER, font=("Consolas", 8, "bold"), anchor="w")
        self._lbl_conn.pack(side="left", padx=4)

        self._surf_row(f, "CPU",  self._v_cpu,  self.BLUE)
        self._surf_row(f, "RAM",  self._v_ram,  self.GREEN)
        self._surf_row(f, "Disk", self._v_disk, self.AMBER)
        self._surf_row(f, "Bat",  self._v_bat,  self.PURPLE)
        self._surf_row(f, "Up",   self._v_up,   self.MUTED)
        self._surf_row(f, "Time", self._v_time, self.MUTED)

        self._canvas = tk.Canvas(f, bg=self.BG, height=60, highlightthickness=0)
        self._canvas.pack(fill="x", pady=(8, 0))

    def _build_terminal(self):
        f = tk.Frame(self._content, bg=self.BG)
        self._frames["term"] = f
        self._out = tk.Text(f, bg="#010409", fg=self.TEXT, font=("Consolas", 8),
                            wrap="word", relief="flat", height=16,
                            insertbackground=self.TEXT)
        self._out.pack(fill="both", expand=True, pady=(0, 4))
        self._out.config(state="disabled")
        row = tk.Frame(f, bg=self.BG)
        row.pack(fill="x")
        self._entry = tk.Entry(row, bg=self.SURF2, fg=self.TEXT,
                               insertbackground=self.TEXT, font=("Consolas", 9),
                               relief="flat", highlightthickness=1,
                               highlightbackground=self.BORDER)
        self._entry.pack(side="left", fill="x", expand=True, ipady=4)
        self._entry.bind("<Return>", self._on_enter)
        tk.Button(row, text="Run", bg=self.BLUE, fg=self.BG,
                  font=("Segoe UI", 8, "bold"), relief="flat",
                  cursor="hand2", command=self._on_enter,
                  padx=6, pady=4).pack(side="right", padx=(4, 0))

    def _build_voice(self):
        f = tk.Frame(self._content, bg=self.BG)
        self._frames["voice"] = f
        lang = (CFG.get("voice") or {}).get("language", "en-US")
        self._v_vstatus = tk.StringVar(value=f"Говорите команду ({lang})")
        self._v_vheard  = tk.StringVar(value="")
        self._v_vresult = tk.StringVar(value="")
        self._btn_mic = tk.Button(f, text="SPEAK", font=("Segoe UI", 14, "bold"),
                                   bg=self.SURF2, fg=self.BLUE, relief="flat",
                                   cursor="hand2", activebackground=self.SURF2,
                                   padx=20, pady=12, command=self._voice_click)
        self._btn_mic.pack(pady=20)
        tk.Label(f, textvariable=self._v_vstatus, bg=self.BG, fg=self.MUTED,
                 font=("Segoe UI", 8)).pack()
        tk.Label(f, text="Heard:", bg=self.BG, fg=self.MUTED,
                 font=("Segoe UI", 7)).pack(pady=(10, 0))
        tk.Label(f, textvariable=self._v_vheard, bg=self.BG, fg=self.TEXT,
                 font=("Segoe UI", 9, "bold"), wraplength=270).pack()
        tk.Label(f, text="Result:", bg=self.BG, fg=self.MUTED,
                 font=("Segoe UI", 7)).pack(pady=(8, 0))
        tk.Label(f, textvariable=self._v_vresult, bg=self.BG, fg=self.GREEN,
                 font=("Segoe UI", 8), wraplength=270, justify="left").pack()
        self._v_vlang = tk.StringVar(value="")
        if voice_engine:
            mic = voice_engine.cfg.get("mic_index", "default")
            self._v_vlang.set(f"Language: {voice_engine.cfg.get('language','en-US')} | Mic: {mic}")
        tk.Label(f, textvariable=self._v_vlang, bg=self.BG, fg=self.MUTED,
                 font=("Consolas", 7)).pack(pady=4)

    def _build_tools(self):
        f = tk.Frame(self._content, bg=self.BG)
        self._frames["tools"] = f
        for label, cmd in [
            ("Screenshot",        "screenshot"),
            ("System Status",     "status"),
            ("CPU / RAM / Disk",  "cpu /and ram /and disk"),
            ("Lock Screen",       "lock"),
            ("Mute Volume",       "volume 0"),
            ("Volume 50%",        "volume 50"),
            ("Clipboard",         "clipboard_get"),
            ("Task Manager",      "task_manager"),
            ("Public IP",         "public_ip"),
            ("Running Processes", "processes 10"),
            ("List Desktop",      "ls Desktop"),
        ]:
            tk.Button(f, text=label, bg=self.SURF2, fg=self.TEXT,
                      font=("Segoe UI", 8), relief="flat",
                      activebackground=self.SURF2, cursor="hand2",
                      anchor="w", padx=10,
                      command=lambda c=cmd: self._tool_run(c)
                      ).pack(fill="x", pady=1, ipady=3)

    def _show_tab(self, name):
        for n, f in self._frames.items(): f.pack_forget()
        for n, b in self._tab_btns.items():
            b.config(bg=self.SURFACE if n != name else self.SURF2,
                     fg=self.MUTED   if n != name else self.BLUE)
        self._frames[name].pack(fill="both", expand=True)

    def _make_draggable(self):
        self._dx = self._dy = 0
        def start(e): self._dx = e.x_root - self.root.winfo_x(); self._dy = e.y_root - self.root.winfo_y()
        def drag(e):  self.root.geometry(f"+{e.x_root-self._dx}+{e.y_root-self._dy}")
        self.root.bind("<Button-1>",  start)
        self.root.bind("<B1-Motion>", drag)

    def _toggle_collapse(self):
        if self._collapsed:
            self._body.pack(fill="both", expand=True)
            self.root.geometry(f"{self.W}x{self.H}")
            self._btn_col.config(text="-"); self._collapsed = False
        else:
            self._body.pack_forget()
            self.root.geometry(f"{self.W}x30")
            self._btn_col.config(text="+"); self._collapsed = True

    def _update_stats(self):
        try:
            if True:
                cpu = psutil.cpu_percent(interval=0)
                mem = psutil.virtual_memory()
                self._v_cpu.set(f"{cpu:.0f}%")
                self._v_ram.set(f"{mem.percent:.0f}%  ({mem.used//1048576}MB / {mem.total//1048576}MB)")
                try:
                    d = psutil.disk_usage("C:\\" if IS_WIN else "/")
                    self._v_disk.set(f"{d.percent:.0f}%")
                except: pass
                try:
                    b = psutil.sensors_battery()
                    self._v_bat.set(f"{b.percent:.0f}% ({'charging' if b.power_plugged else 'battery'})" if b else "N/A")
                except: self._v_bat.set("N/A")
                boot = datetime.fromtimestamp(psutil.boot_time())
                self._v_up.set(str(datetime.now()-boot).split(".")[0])

                c = self._canvas
                c.delete("all")
                bars = [("CPU", cpu, self.BLUE), ("RAM", mem.percent, self.GREEN)]
                try: bars.append(("Disk", psutil.disk_usage("C:\\" if IS_WIN else "/").percent, self.AMBER))
                except: pass
                cw = self.W - 24; ch = 58; bw = cw // len(bars) - 10
                for i, (lbl, val, col) in enumerate(bars):
                    x0 = i * (bw + 10) + 4
                    bh = max(2, int((val / 100) * (ch - 20)))
                    c.create_rectangle(x0, ch-bh-8, x0+bw, ch-8, fill=col, outline="")
                    c.create_text(x0+bw//2, ch-2, text=lbl, fill=self.MUTED, font=("Segoe UI", 6))
                    c.create_text(x0+bw//2, ch-bh-14, text=f"{val:.0f}%", fill=col, font=("Segoe UI", 6, "bold"))

            self._v_time.set(datetime.now().strftime("%H:%M:%S"))
            if _ws_connected:
                self._v_conn.set("Connected"); self._lbl_conn.config(fg=self.GREEN)
            else:
                self._v_conn.set("Reconnecting..."); self._lbl_conn.config(fg=self.RED)
        except Exception as e:
            log.debug(f"stats: {e}")
        self.root.after(2000, self._update_stats)

    def _on_enter(self, _event=None):
        cmd = self._entry.get().strip()
        if not cmd: return
        self._entry.delete(0, "end")
        self._log(f">>> {cmd}", self.BLUE)
        self._submit(cmd)

    def _log(self, text: str, color: str = None):
        self._out.config(state="normal")
        tag = f"col_{color}" if color else "plain"
        self._out.tag_config(tag, foreground=color or self.TEXT)
        self._out.insert("end", text + "\n", tag)
        self._out.see("end")
        self._out.config(state="disabled")

    def _voice_click(self):
        if self._listening: return
        self._listening = True
        self._btn_mic.config(fg=self.RED, text="LISTENING...")
        self._v_vstatus.set("Listening... speak now")
        threading.Thread(target=self._voice_worker, daemon=True).start()

    def _voice_worker(self):
        text = _listen_once(timeout=6)
        self.root.after(0, self._voice_done, text)

    def _voice_done(self, text: str):
        self._listening = False
        self._btn_mic.config(fg=self.BLUE, text="SPEAK")
        if text:
            self._v_vstatus.set("Command received")
            self._v_vheard.set(f'"{text}"')
            self._v_vresult.set("Running...")
            self._show_tab("voice")
            # Проверить голосовые триггеры плагинов
            cmd = text
            if _PLUGINS_AVAILABLE and plugin_manager:
                matched = plugin_manager.match_voice(text)
                if matched:
                    cmd = matched
                    self._v_vheard.set(f'"{text}" → {matched}')
            self._submit(cmd)
            _speak(f"Running: {cmd}")
        else:
            self._v_vstatus.set("Nothing heard — try again")

    def _tool_run(self, cmd: str):
        self._show_tab("term")
        self._log(f">>> {cmd}", self.AMBER)
        self._submit(cmd)

    def _poll_results(self):
        try:
            while True:
                cmd, result = self._rq.get_nowait()
                self._log(result, self.GREEN)
                self._v_vresult.set(result[:300])
                self._v_vstatus.set("Done")
        except _queue.Empty:
            pass
        except Exception:
            pass
        self.root.after(150, self._poll_results)

    def run(self):
        self.root.mainloop()


# ─────────────────────────────────────────────────────────────────────────────
#  WORKER — runs inside the watchdog child process. ALWAYS HEADLESS.
#  The GUI lives in a completely separate process (gui_panel.py) and talks to
#  this worker over the local IPC socket — closing the GUI window never
#  touches this process, so the connection to the server never drops.
# ─────────────────────────────────────────────────────────────────────────────
def _worker_main():
    global CFG, _ws_connected

    if apply_pending_update():
        os._exit(0)

    CFG = load_config()
    if not CFG.get("name"):
        name = ask_name_once() or f"pc_{socket.gethostname()}"
        CFG["name"] = name
        save_config(CFG)
        log.info(f"PC name saved: {name}")
    if not CFG.get("secret_key"):
        log.error("FATAL: secret_key is empty in config.json — set it before running")
        sys.exit(1)
    log.info(f"Starting v{CLIENT_VERSION} as '{CFG['name']}' — {len(COMMANDS)} commands")

    global voice_engine
    voice_engine = VoiceEngine(CFG)
    voice_status = voice_engine.initialize()
    log.info(voice_status)

    # Plugin system ready — плагины грузятся в _run_all

    stop_evt = threading.Event()

    async def _run_all():
        # Загрузить плагины до первого подключения к серверу
        if _PLUGINS_AVAILABLE and plugin_manager:
            loaded = await plugin_manager.load_all()
            if loaded:
                log.info(f"Plugins ready: {', '.join(loaded)}")
        ipc_t = asyncio.create_task(ipc_server_loop())
        pw_t  = asyncio.create_task(post_update_watchdog(stop_evt))
        try:
            await ws_loop(stop_evt)
        finally:
            ipc_t.cancel()
            pw_t.cancel()

    try:
        asyncio.run(_run_all())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.error(f"Fatal worker error: {e}")
        raise  # non-zero exit → watchdog restarts us


# ─────────────────────────────────────────────────────────────────────────────
#  WATCHDOG + ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
def main():
    # MUST be first — required for frozen exe multiprocessing on Windows
    multiprocessing.freeze_support()

    if getattr(sys, "frozen", False):
        # Running as .exe — use watchdog
        log.info("Watchdog started")
        crash_streak = 0
        while True:
            start_t = _time.time()
            p = multiprocessing.Process(target=_worker_main, daemon=False)
            p.start()
            log.info(f"Worker PID: {p.pid}")
            p.join()
            code = p.exitcode
            elapsed = _time.time() - start_t

            if code == 0:
                log.info("Worker exited cleanly — stopping")
                break

            # FIX: if we are in the middle of an update (update_applied.flag
            # present) and the new worker keeps dying within seconds of
            # starting, the new build is broken in a way that never even
            # reaches the normal post_update_watchdog connectivity check
            # (e.g. it crashes before asyncio starts). Detect that crash
            # loop here and roll back instead of restarting forever.
            if _UPDATE_APPLIED_FLAG.exists() and elapsed < 15:
                crash_streak += 1
                log.warning(f"Worker crashed {elapsed:.1f}s after start (code {code}), "
                            f"crash_streak={crash_streak}")
                if crash_streak >= 3:
                    log.error("Repeated immediate crashes right after an update — rolling back")
                    if rollback_to_previous_version("repeated crash-on-start after update"):
                        break
                    crash_streak = 0
            else:
                crash_streak = 0

            log.warning(f"Worker exited (code {code}) — restarting in 3s")
            _time.sleep(3)
    else:
        # Dev mode — run directly
        _worker_main()


if __name__ == "__main__":
    main()
