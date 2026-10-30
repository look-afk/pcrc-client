"""
Remote Control — GUI Panel (отдельный процесс)
Подключается к headless-воркеру (client_final.py) через локальный IPC.
Закрытие этого окна НЕ останавливает воркер и соединение с сервером —
просто запустите RemoteControlPanel.exe заново, когда понадобится панель.
"""

import asyncio
import json
import queue as _queue
import socket
import sys
import threading
import time
from pathlib import Path

# Модуль ядра лежит рядом (в собранном виде — оба .exe из одного репозитория)
import client_final as cf
from client_final import FloatingPanel

RECONNECT_DELAY = 3


class IPCClient:
    """Фоновый поток: держит TCP-соединение с воркером, шлёт команды, тянет статистику."""

    def __init__(self, cmd_queue: "_queue.Queue", result_queue: "_queue.Queue"):
        self._cmd_q = cmd_queue
        self._res_q = result_queue
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="IPCClient").start()

    def stop(self):
        self._stop.set()

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception as e:
            cf.log.error(f"IPC client fatal: {e}")
        finally:
            loop.close()

    async def _main(self):
        while not self._stop.is_set():
            try:
                reader, writer = await asyncio.open_connection(cf.IPC_HOST, cf.IPC_PORT)
            except (ConnectionRefusedError, OSError):
                cf._ws_connected = False
                await asyncio.sleep(RECONNECT_DELAY)
                continue

            try:
                auth = json.dumps({"key": cf.CFG.get("secret_key", "")}) + "\n"
                writer.write(auth.encode("utf-8"))
                await writer.drain()

                stats_t = asyncio.create_task(self._stats_pump(writer))
                cmd_t = asyncio.create_task(self._cmd_pump(writer))
                try:
                    while not self._stop.is_set():
                        line = await reader.readline()
                        if not line:
                            break
                        msg = json.loads(line.decode("utf-8"))
                        if msg.get("type") == "result":
                            self._res_q.put((msg.get("cmd", ""), msg.get("result", "")))
                        elif msg.get("type") == "stats":
                            cf._ws_connected = bool(msg.get("connected"))
                finally:
                    stats_t.cancel()
                    cmd_t.cancel()
                    writer.close()
            except Exception as e:
                cf.log.debug(f"IPC session error: {e}")
            cf._ws_connected = False
            await asyncio.sleep(RECONNECT_DELAY)

    async def _stats_pump(self, writer):
        while True:
            try:
                writer.write((json.dumps({"type": "stats"}) + "\n").encode("utf-8"))
                await writer.drain()
            except Exception:
                return
            await asyncio.sleep(2)

    async def _cmd_pump(self, writer):
        while True:
            try:
                cmd = self._cmd_q.get_nowait()
            except _queue.Empty:
                await asyncio.sleep(0.1)
                continue
            try:
                writer.write((json.dumps({"type": "cmd", "cmd": cmd}) + "\n").encode("utf-8"))
                await writer.drain()
            except Exception:
                return


def _not_configured_dialog():
    """
    FIX: this process is built with --windowed (no console), so the old
    print()+sys.exit(1) was invisible — the panel just vanished with no
    explanation. RemoteControl.exe (the headless core) is the one that
    performs first-run setup (asks for name + secret_key) and writes
    config.json; if someone opens the Panel exe before that has happened,
    show them an actual window telling them what to do instead of silently
    closing.
    """
    import tkinter as tk
    root = tk.Tk()
    root.title("Remote Control Panel")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    w, h = 420, 170
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
    root.configure(bg="#0d1117")
    tk.Label(root, text="Not set up yet", bg="#0d1117", fg="#f85149",
             font=("Segoe UI", 12, "bold")).pack(pady=(20, 6))
    tk.Label(
        root,
        text="Run RemoteControl.exe first — it will ask for this PC's\n"
             "name and the server's secret key, and only then can\n"
             "the panel connect to it.",
        bg="#0d1117", fg="#e6edf3", font=("Segoe UI", 9), justify="center",
    ).pack(pady=(0, 16))
    tk.Button(root, text="OK", command=root.destroy,
              bg="#58a6ff", fg="#0d1117", relief="flat",
              font=("Segoe UI", 10, "bold"), padx=16, pady=5,
              cursor="hand2").pack()
    root.mainloop()


def main():
    cf.CFG = cf.load_config()
    if not cf.CFG.get("secret_key") or not cf.CFG.get("name"):
        _not_configured_dialog()
        sys.exit(1)

    cmd_q = _queue.Queue()
    res_q = _queue.Queue()

    ipc = IPCClient(cmd_q, res_q)
    ipc.start()

    panel = FloatingPanel(cmd_q.put, res_q)
    panel.run()  # закрытие окна просто завершает этот процесс — воркер не трогаем

    ipc.stop()


if __name__ == "__main__":
    main()