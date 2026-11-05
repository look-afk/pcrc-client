"""Патч client_final.py: буфер обмена (текст, картинки, файлы), обмен ПК <-> ПК и Telegram <-> ПК."""
import ast, re, shutil, sys
from pathlib import Path


class Patcher:
    """Точные замены. Ничего не пишет на диск, если хоть один якорь не найден или результат не компилируется."""

    def __init__(self, src: str):
        self.src, self.errors, self.done = src, [], []

    def sub(self, label, old, new):
        if new in self.src:
            self.done.append(f"= {label} (уже применено)"); return
        n = self.src.count(old)
        if n != 1:
            self.errors.append(f"x {label}: якорь найден {n} раз (нужен ровно 1)"); return
        self.src = self.src.replace(old, new); self.done.append(f"+ {label}")

    def before(self, label, anchor, block):
        self.sub(label, anchor, block + anchor)

    def rx(self, label, pattern, new, flags=re.M, done_marker=None):
        if (done_marker or new.strip()) in self.src:
            self.done.append(f"= {label} (уже применено)"); return
        out, n = re.subn(pattern, lambda m: new, self.src, count=1, flags=flags)
        if n != 1:
            self.errors.append(f"x {label}: шаблон не найден"); return
        self.src = out; self.done.append(f"+ {label}")

    def func(self, name, new_code):
        self.rx(f"функция {name}", rf"^(?:async )?def {name}\(.*?\n(?:(?:[ \t].*)?\n)*", new_code.rstrip() + "\n\n\n",
                done_marker=new_code.strip())


def run(apply):
    if len(sys.argv) != 2:
        print(f"Использование: python {Path(sys.argv[0]).name} <путь к файлу>"); sys.exit(2)
    path = Path(sys.argv[1])
    raw = path.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    p = Patcher(raw.replace("\r\n", "\n"))
    apply(p)
    print("\n".join(p.done + p.errors))
    if p.errors:
        print("\nНИЧЕГО НЕ ЗАПИСАНО: часть якорей не найдена (файл отличается от ожидаемого). Пришлите вывод выше."); sys.exit(1)
    try:
        ast.parse(p.src)
    except SyntaxError as e:
        print(f"\nНИЧЕГО НЕ ЗАПИСАНО: результат не компилируется: {e}"); sys.exit(1)
    shutil.copy2(path, str(path) + ".bak")
    path.write_bytes((p.src.replace("\n", "\r\n") if crlf else p.src).encode("utf-8"))
    print(f"\nГотово. Резервная копия: {path}.bak")


CLIENT_BLOCK = r'''# ─────────────────────────────────────────────────────────────────────────────
#  CLIPBOARD HUB — текст, картинки и файлы; ПК <-> ПК и Telegram <-> ПК
# ─────────────────────────────────────────────────────────────────────────────
_CLIP_MAX_BYTES  = 20 * 1024 * 1024    # тот же потолок, что и у /upload на сервере
_CLIP_TEXT_LIMIT = 3500                # более длинный текст возвращается ссылкой на .txt
_DL_DIR          = Path.home() / "Downloads" / "PCRC"
_NOWIN           = subprocess.CREATE_NO_WINDOW if IS_WIN else 0


def _server_http_base() -> str:
    base = (CFG.get("server_url") or "").strip().rstrip("/")
    if base.startswith("wss://"): base = "https://" + base[6:]
    elif base.startswith("ws://"): base = "http://" + base[5:]
    if base.endswith("/ws"): base = base[:-3]
    return base


def _upload_own(data: bytes, filename: str) -> str:
    """Загрузка только на НАШ сервер: содержимое буфера не уходит на сторонние хостинги."""
    base = _server_http_base()
    if not base:
        raise RuntimeError("server_url is not configured")
    r = requests.post(f"{base}/upload", files={"file": (filename, data)},
                      headers={"X-Secret": CFG.get("secret_key", "")}, timeout=30)
    url = r.json().get("url", "") if r.status_code == 200 else ""
    if not url:
        raise RuntimeError(f"upload to server failed (HTTP {r.status_code})")
    return url


def _looks_like_image(data: bytes) -> bool:
    return (data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff" or data[:4] == b"GIF8"
            or data[:2] == b"BM" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def _clip_read():
    """('image', bytes, w, h) | ('files', [Path]) | ('text', str) | (None,)"""
    if IS_WIN or IS_MAC:
        try:
            got = ImageGrab.grabclipboard()
        except Exception as e:
            log.debug(f"grabclipboard: {e}")
            got = None
        if isinstance(got, Image.Image):
            buf = io.BytesIO(); got.save(buf, "PNG"); data = buf.getvalue()
            if len(data) > _CLIP_MAX_BYTES:
                buf = io.BytesIO(); got.convert("RGB").save(buf, "JPEG", quality=88); data = buf.getvalue()
            return ("image", data, got.width, got.height)
        if isinstance(got, list):
            files = [Path(x) for x in got if Path(x).is_file()]
            if files:
                return ("files", files)
    try:
        text = pyperclip.paste()
    except Exception:
        text = ""
    return ("text", text) if text else (None,)


def _clip_write_text(text: str) -> None:
    try:
        pyperclip.copy(text)
    except Exception:
        if not IS_WIN:
            raise
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "Set-Clipboard -Value $env:PCRC_CLIP"],
                       env={**os.environ, "PCRC_CLIP": text[:30000]}, timeout=20,
                       creationflags=_NOWIN, check=True)


def _clip_ps_sta(script: str, value: str, what: str) -> None:
    """PowerShell в STA-режиме (нужен для буфера обмена). Данные передаются через переменную окружения."""
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
                       env={**os.environ, "PCRC_CLIP_FILE": value}, capture_output=True, text=True,
                       timeout=30, creationflags=_NOWIN)
    if r.returncode != 0:
        raise RuntimeError(f"{what}: {(r.stderr or r.stdout).strip()[:200] or 'failed'}")


def _clip_write_image(data: bytes):
    if not IS_WIN:
        raise RuntimeError("image clipboard is supported on Windows only")
    img = Image.open(io.BytesIO(data)); w, h = img.size
    tmp = Path(tempfile.gettempdir()) / f"pcrc_clip_{os.getpid()}.png"
    img.convert("RGBA" if "A" in img.getbands() else "RGB").save(tmp, "PNG")   # jpg/gif/bmp/webp -> png
    try:
        _clip_ps_sta("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                     "$i=[System.Drawing.Image]::FromFile($env:PCRC_CLIP_FILE);"
                     "[System.Windows.Forms.Clipboard]::SetImage($i);$i.Dispose()", str(tmp), "set image")
    finally:
        tmp.unlink(missing_ok=True)
    return w, h


def _clip_write_files(paths) -> None:
    if not IS_WIN:
        raise RuntimeError("file clipboard is supported on Windows only")
    _clip_ps_sta("Set-Clipboard -LiteralPath $env:PCRC_CLIP_FILE", str(paths[0]), "set file")


async def cmd_clip_get(p):
    """Что сейчас в буфере: текст приходит текстом, картинка - фото, файл - документом."""
    try:
        item = await asyncio.to_thread(_clip_read)
        if item[0] == "image":
            _, data, w, h = item
            out = await asyncio.to_thread(_format_screenshot, data, w, h)
            return out + "\nClipboard image" if out.startswith("OK ") else out
        if item[0] == "files":
            files = item[1]; f = files[0]; size = f.stat().st_size
            if len(files) == 1 and size <= _CLIP_MAX_BYTES:
                link = await asyncio.to_thread(_upload_own, f.read_bytes(), f.name)
                return f"Ready!\n{f.name} ({mb(size)})\n{link}"
            lines = [f"Clipboard: {len(files)} file(s)"] + [f"  {x.name} ({mb(x.stat().st_size)})" for x in files[:20]]
            return "\n".join(lines + ["Only a single file up to 20 MB can be fetched; zip several files first."])
        if item[0] == "text":
            text = item[1]
            if len(text) <= _CLIP_TEXT_LIMIT:
                return f"Clipboard ({len(text)} chars):\n{text}"
            raw = text.encode("utf-8")
            link = await asyncio.to_thread(_upload_own, raw, "clipboard.txt")
            return f"Ready!\nclipboard.txt ({mb(len(raw))})\n{link}"
        return "Clipboard is empty"
    except Exception as e:
        return f"Error: {e}"


async def cmd_clip_set(p):
    if not p.strip():
        return "Usage: clip_set <text>"
    try:
        await asyncio.to_thread(_clip_write_text, p)
        return f"Clipboard set ({len(p)} chars)"
    except Exception as e:
        return f"Error: {e}"


async def cmd_clip_send(p):
    """clip_send <computer>: отправить мой буфер (текст / картинку / один файл) на другой компьютер."""
    parts = p.split()
    if not parts:
        return "Usage: clip_send <computer>"
    target = parts[0]
    if target == CFG.get("name"):
        return "That is this computer."
    try:
        item = await asyncio.to_thread(_clip_read)
        if item[0] is None:
            return "Clipboard is empty"
        if item[0] == "text":
            data, name, kind, summary = item[1].encode("utf-8"), "clipboard.txt", "text", f"text, {len(item[1])} chars"
        elif item[0] == "image":
            data = item[1]
            name = "clipboard." + ("jpg" if data[:3] == b"\xff\xd8\xff" else "png")
            kind, summary = "image", f"image {item[2]}x{item[3]}"
        else:
            if len(item[1]) != 1:
                return "Only a single file can be sent. Zip several files first (zip_folder)."
            f = item[1][0]
            if f.stat().st_size > _CLIP_MAX_BYTES:
                return f"File too large ({mb(f.stat().st_size)}, max 20 MB)"
            data, name, kind, summary = f.read_bytes(), f.name, "file", f"file {f.name}"
        if len(data) > _CLIP_MAX_BYTES:
            return f"Too large ({mb(len(data))}, max 20 MB)"
        link = await asyncio.to_thread(_upload_own, data, name)
        return f"[CLIPSEND]{target}\n{kind}\n{link}\n{summary}"
    except Exception as e:
        return f"Error: {e}"


async def cmd_clip_put(p):
    """clip_put <url> [text|image|file|auto]: скачать с СВОЕГО сервера и положить в буфер."""
    parts = p.split()
    url = parts[0] if parts else ""
    hint = parts[1].lower() if len(parts) > 1 else "auto"
    base = _server_http_base()
    if not url or not base or not url.startswith(base + "/dl/"):
        return "Error: clip_put accepts only one-time links from this PCRC server (/dl/...)"
    if hint not in ("auto", "text", "image", "file"):
        hint = "auto"
    try:
        r = await asyncio.to_thread(requests.get, url, timeout=60)
        if r.status_code != 200:
            return f"Error: download failed (HTTP {r.status_code}) - the link expired or was already used"
        data = r.content
        import re as _re
        m = _re.search(r'filename="([^"]+)"', r.headers.get("Content-Disposition", ""))
        name = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(m.group(1)).name if m else "file.bin").lstrip(". ")
        name = _re.sub(r"\.{2,}", "_", name)[:120] or "file.bin"
        if hint == "auto":
            hint = "image" if _looks_like_image(data) else "file"
        if hint == "text":
            text = data.decode("utf-8", errors="replace")
            await asyncio.to_thread(_clip_write_text, text)
            return f"Clipboard set: text ({len(text)} chars)"
        if hint == "image":
            w, h = await asyncio.to_thread(_clip_write_image, data)
            return f"Clipboard set: image {w}x{h} - paste with Ctrl+V"
        _DL_DIR.mkdir(parents=True, exist_ok=True)
        dest, n = _DL_DIR / name, 1
        while dest.exists():
            dest = _DL_DIR / f"{Path(name).stem}_{n}{Path(name).suffix}"; n += 1
        dest.write_bytes(data)
        try:
            await asyncio.to_thread(_clip_write_files, [dest])
        except Exception as e:
            return f"File saved to {dest} (clipboard not set: {e})"
        return f"Clipboard set: file {dest.name} (saved to {dest}) - paste with Ctrl+V"
    except Exception as e:
        return f"Error: {e}"


'''

REGISTER = '''COMMANDS["plugin_delete"]   = cmd_plugin_delete

# Clipboard hub. clipboard_get / clipboard_set остаются как псевдонимы новых команд.
COMMANDS["clip_get"]        = cmd_clip_get
COMMANDS["clip_set"]        = cmd_clip_set
COMMANDS["clip_send"]       = cmd_clip_send
COMMANDS["clip_put"]        = cmd_clip_put
COMMANDS["clipboard_get"]   = cmd_clip_get
COMMANDS["clipboard_set"]   = cmd_clip_set'''

HELP_NEW = ("CLIPBOARD   : clip_get, clip_set, clip_send <pc>, clip_put <url>, clipboard_history\n"
            "              (clipboard_get / clipboard_set - aliases; text, images and files)")


def apply(p):
    p.before("буфер: команды и помощники", "# 10. NOTIFICATIONS", CLIENT_BLOCK)
    p.sub("буфер: регистрация команд", 'COMMANDS["plugin_delete"]   = cmd_plugin_delete', REGISTER)
    p.sub("буфер: справка", "CLIPBOARD   : clipboard_get, clipboard_set, clipboard_history", HELP_NEW)


if __name__ == "__main__":
    run(apply)
