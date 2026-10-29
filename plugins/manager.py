"""
Remote Control — менеджер плагинов.
Загружает все плагины из папки plugins/, регистрирует их команды и голосовые триггеры.
"""

import asyncio
import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from plugins._base import PluginBase

log = logging.getLogger("rc.plugins")

def _get_plugins_dir() -> Path:
    """Найти папку plugins/ рядом с exe (frozen) или рядом с этим файлом (dev)."""
    if getattr(sys, "frozen", False):
        # Запущен как PyInstaller exe — ищем рядом с exe
        return Path(sys.executable).parent / "plugins"
    # Dev режим — рядом с manager.py
    return Path(__file__).parent

PLUGINS_DIR = _get_plugins_dir()


class PluginManager:
    def __init__(self):
        self._plugins: dict[str, PluginBase] = {}   # name -> instance
        self._cmd_map: dict[str, str] = {}           # command -> plugin name
        self._voice_map: dict[str, str] = {}         # phrase -> command

    # ── Загрузка всех плагинов из папки ──────────────────────────────────────

    async def load_all(self) -> list[str]:
        """Загрузить все плагины из plugins/. Возвращает список имён."""
        loaded = []
        for path in sorted(PLUGINS_DIR.glob("*.py")):
            if path.stem.startswith("_"):
                continue
            name = await self._load_file(path)
            if name:
                loaded.append(name)
        if loaded:
            log.info(f"Plugins loaded: {', '.join(loaded)}")
        return loaded

    async def _load_file(self, path: Path) -> str | None:
        """Загрузить один .py файл как плагин. Возвращает имя или None при ошибке."""
        try:
            spec = importlib.util.spec_from_file_location(f"plugin_{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            # Найти класс — наследник PluginBase
            plugin_cls = None
            for attr in vars(mod).values():
                if isinstance(attr, type) and issubclass(attr, PluginBase) and attr is not PluginBase:
                    plugin_cls = attr
                    break

            if not plugin_cls:
                log.warning(f"No PluginBase subclass in {path.name} — skipped")
                return None

            instance = plugin_cls()
            if not instance.NAME:
                log.warning(f"Plugin in {path.name} has no NAME — skipped")
                return None

            # Выгрузить старую версию если была
            if instance.NAME in self._plugins:
                await self.unload(instance.NAME)

            await instance.on_load()
            self._plugins[instance.NAME] = instance

            # Зарегистрировать команды
            for cmd in instance.COMMANDS:
                self._cmd_map[cmd] = instance.NAME

            # Зарегистрировать голосовые триггеры
            for phrase, cmd in instance.VOICE_TRIGGERS.items():
                self._voice_map[phrase.lower()] = cmd

            log.info(f"Plugin loaded: {instance.NAME} v{instance.VERSION} ({len(instance.COMMANDS)} commands)")
            return instance.NAME

        except Exception as e:
            log.error(f"Failed to load plugin {path.name}: {e}")
            return None

    # ── Установка по URL ──────────────────────────────────────────────────────

    async def install(self, url: str, sha256: str = "") -> str:
        """Скачать плагин по URL, проверить sha256, загрузить."""
        import hashlib
        import requests
        url = url.strip()
        sha256 = (sha256 or "").strip().lower()
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            return ("Usage: plugin_install <url> <sha256>\n"
                    "sha256 is REQUIRED (certutil -hashfile plugin.py SHA256).")
        try:
            r = requests.get(url, timeout=15)
            if r.status_code != 200:
                return f"Error: HTTP {r.status_code}"

            actual = hashlib.sha256(r.content).hexdigest()
            if actual != sha256:
                return (f"Error: sha256 mismatch — expected {sha256}, "
                        f"got {actual}. File NOT installed.")

            # Имя файла из URL
            fname = url.rstrip("/").split("/")[-1]
            if not fname.endswith(".py"):
                fname += ".py"
            if fname.startswith("_"):
                return "Error: plugin filename cannot start with _"

            dest = PLUGINS_DIR / fname
            dest.write_bytes(r.content)

            name = await self._load_file(dest)
            if name:
                return f"Plugin '{name}' installed and loaded."
            else:
                dest.unlink(missing_ok=True)
                return "Error: file downloaded but no valid PluginBase class found."
        except Exception as e:
            return f"Error installing plugin: {e}"

    # ── Выгрузка ──────────────────────────────────────────────────────────────

    async def unload(self, name: str) -> str:
        plugin = self._plugins.pop(name, None)
        if not plugin:
            return f"Plugin '{name}' not loaded."
        try:
            await plugin.on_unload()
        except Exception:
            pass
        # Убрать команды
        self._cmd_map = {k: v for k, v in self._cmd_map.items() if v != name}
        self._voice_map = {k: v for k, v in self._voice_map.items()
                           if v not in plugin.COMMANDS}
        log.info(f"Plugin unloaded: {name}")
        return f"Plugin '{name}' unloaded."

    async def delete(self, name: str) -> str:
        """Выгрузить и удалить файл плагина."""
        plugin = self._plugins.get(name)
        if not plugin:
            return f"Plugin '{name}' not loaded."
        result = await self.unload(name)
        for path in PLUGINS_DIR.glob("*.py"):
            if path.stem.startswith("_"):
                continue
            # Проверяем совпадение по NAME внутри файла
            try:
                text = path.read_text(encoding="utf-8")
                if f'NAME = "{name}"' in text or f"NAME = '{name}'" in text:
                    path.unlink()
                    return f"Plugin '{name}' deleted."
            except Exception:
                pass
        return result + " (file not found to delete)"

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def is_plugin_cmd(self, cmd_name: str) -> bool:
        return cmd_name in self._cmd_map

    async def dispatch(self, cmd_name: str, param: str) -> str:
        plugin_name = self._cmd_map.get(cmd_name)
        if not plugin_name:
            return f"No plugin handles command '{cmd_name}'"
        plugin = self._plugins.get(plugin_name)
        if not plugin:
            return f"Plugin '{plugin_name}' is not loaded"
        try:
            return str(await plugin.handle(cmd_name, param))
        except Exception as e:
            return f"Plugin '{plugin_name}' error: {e}"

    # ── Голосовые триггеры ────────────────────────────────────────────────────

    def match_voice(self, text: str) -> str | None:
        """Найти команду по голосовой фразе. Возвращает команду или None."""
        text = text.lower().strip()
        for phrase, cmd in self._voice_map.items():
            if phrase in text:
                return cmd
        return None

    # ── Инфо ──────────────────────────────────────────────────────────────────

    def list_plugins(self) -> list[dict]:
        result = []
        for name, p in self._plugins.items():
            result.append({
                "name":         name,
                "version":      p.VERSION,
                "description":  p.DESCRIPTION,
                "commands":     list(p.COMMANDS.keys()),
                "voice_triggers": len(p.VOICE_TRIGGERS),
                "show_button":  p.SHOW_BUTTON,
                "button_label": p.BUTTON_LABEL,
                "button_cmd":   p.BUTTON_CMD,
            })
        return result

    def all_commands(self) -> dict[str, str]:
        """Все команды всех плагинов: cmd -> описание."""
        out = {}
        for name, plugin in self._plugins.items():
            for cmd, desc in plugin.COMMANDS.items():
                out[cmd] = f"[{name}] {desc}"
        return out

    @property
    def voice_map(self) -> dict[str, str]:
        return self._voice_map


# Глобальный экземпляр
plugin_manager = PluginManager()
