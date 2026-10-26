# app/interfaces/cli_adapter.py
dv_file_version = '261025.01'
"""Консольный адаптер для быстрой отладки ядра repdix."""
import asyncio

from app.core.engine import RepdixEngine
from app.interfaces.base import BaseChatInterface, ChatMessage


class CLIAdapter(BaseChatInterface):
    """Адаптер CLI: читает ввод из stdin, печатает ответы в stdout."""

    def __init__(self, engine: RepdixEngine) -> None:
        self.engine = engine

    async def start(self) -> None:
        """Цикл чтения команд из консоли."""
        while True:
            try:
                user_input = await asyncio.to_thread(input, "> ")
            except (EOFError, KeyboardInterrupt):
                break

            if user_input == "exit":
                break

            msg = ChatMessage(user_id="cli", chat_id="cli", text=user_input)
            result = await self.engine.process_message(msg)
            print(result.text)

    async def send_message(self, chat_id: str, text: str) -> None:
        """Вывод текстового сообщения в консоль."""
        print(text)

    async def send_image(self, chat_id: str, image_path: str) -> None:
        """Вывод пути к изображению в консоль."""
        print(image_path)
