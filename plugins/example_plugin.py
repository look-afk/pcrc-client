"""
Remote Control — пример плагина.
Скопируйте этот файл, переименуйте и измените под себя.
"""

from plugins._base import PluginBase


class ExamplePlugin(PluginBase):
    NAME        = "example"
    VERSION     = "1.0"
    DESCRIPTION = "Пример плагина"

    COMMANDS = {
        "example_hello":   "Поприветствовать",
        "example_time":    "Показать время",
        "example_reverse": "Перевернуть текст: example_reverse <текст>",
    }

    # Кнопка появится в Telegram у привязанного юзера
    SHOW_BUTTON  = True
    BUTTON_LABEL = "👋 Hello"
    BUTTON_CMD   = "example_hello"

    VOICE_TRIGGERS = {
        "привет компьютер": "example_hello",
        "который час":      "example_time",
    }

    async def handle(self, cmd: str, param: str) -> str:
        if cmd == "example_hello":
            return "Привет! Плагин example работает."
        if cmd == "example_time":
            from datetime import datetime
            return f"Сейчас: {datetime.now().strftime('%H:%M:%S %d.%m.%Y')}"
        if cmd == "example_reverse":
            if not param:
                return "Использование: example_reverse <текст>"
            return param[::-1]
        return f"Неизвестная команда: {cmd}"
