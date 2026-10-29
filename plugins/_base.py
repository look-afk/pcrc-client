"""
Remote Control — базовый класс плагина.

Минимальный плагин:
    from plugins._base import PluginBase

    class MyPlugin(PluginBase):
        NAME        = "my_plugin"
        VERSION     = "1.0"
        DESCRIPTION = "Что делает плагин"

        COMMANDS = {
            "my_cmd": "Описание команды",
        }

        # Показывать ли кнопку в Telegram у привязанного юзера?
        SHOW_BUTTON  = True
        BUTTON_LABEL = "🔧 My Command"   # текст кнопки
        BUTTON_CMD   = "my_cmd"          # какую команду вызывает кнопка

        async def handle(self, cmd: str, param: str) -> str:
            if cmd == "my_cmd":
                return "Привет от плагина!"
            return f"Unknown command: {cmd}"

        VOICE_TRIGGERS = {
            "привет компьютер": "my_cmd",
        }
"""


class PluginBase:
    NAME: str        = ""
    VERSION: str     = "1.0"
    DESCRIPTION: str = ""

    # Команды плагина: "command_name" -> "Описание"
    COMMANDS: dict[str, str] = {}

    # Голосовые триггеры: "фраза" -> "command_name"
    VOICE_TRIGGERS: dict[str, str] = {}

    # Кнопка в Telegram для привязанного юзера
    SHOW_BUTTON: bool  = False   # True = кнопка появится в меню юзера
    BUTTON_LABEL: str  = ""      # текст кнопки (с эмодзи)
    BUTTON_CMD: str    = ""      # команда которую вызывает кнопка

    async def handle(self, cmd: str, param: str) -> str:
        return f"[{self.NAME}] command '{cmd}' not implemented"

    async def on_load(self) -> None:
        pass

    async def on_unload(self) -> None:
        pass
