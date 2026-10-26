# app/interfaces/telegram_adapter.py
dv_file_version = '261025.02'
"""Telegram-адаптер на aiogram 3."""
import os

import yaml
from aiogram import Bot, Dispatcher, types
from aiogram.types import FSInputFile

from app.core.engine import RepdixEngine
from app.interfaces.base import BaseChatInterface, ChatMessage, ChatResponse

class TelegramAdapter(BaseChatInterface):
    """Адаптер Telegram на базе aiogram 3."""

    def __init__(self, engine: RepdixEngine) -> None:
        self.engine = engine
        self.bot = Bot(token=os.environ["TELEGRAM_TOKEN"])
        self.dp = Dispatcher()

        self.users_yaml_path = "config/users.yaml"
        self.users_yaml_mtime: float | None = None

        allowed_users_raw = os.environ.get("ALLOWED_USERS", "")
        self.allowed_users: set[int] = {
            int(uid) for uid in allowed_users_raw.split(",") if uid.strip()
        }

        self._load_users_yaml()

    def _load_users_yaml(self) -> None:
        """Загружает список разрешённых пользователей из config/users.yaml."""
        if not os.path.exists(self.users_yaml_path):
            self.users_yaml_mtime = None
            return

        try:
            with open(self.users_yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            users = data.get("users", []) if data else []
            for user in users:
                uid = user.get("id")
                if uid is not None:
                    self.allowed_users.add(int(uid))
            self.users_yaml_mtime = os.path.getmtime(self.users_yaml_path)
        except Exception:
            self.users_yaml_mtime = None

    async def start(self) -> None:
        """Регистрация хэндлеров и запуск polling."""
        self.dp.message.register(self.handle_message)
        await self.dp.start_polling(self.bot)

    async def handle_message(self, message: types.Message) -> None:
        """Обработка входящего текстового сообщения."""
        if os.path.exists(self.users_yaml_path):
            current_mtime = os.path.getmtime(self.users_yaml_path)
            if self.users_yaml_mtime is None or current_mtime > self.users_yaml_mtime:
                self._load_users_yaml()

        if self.allowed_users and message.from_user.id not in self.allowed_users:
            await message.answer("Доступ запрещён")
            return

        msg = ChatMessage(
            user_id=str(message.from_user.id),
            chat_id=str(message.chat.id),
            text=message.text or "",
        )
        result: ChatResponse = await self.engine.process_message(msg)

        await message.answer(result.text)

        if result.image_path:
            await message.answer_photo(photo=FSInputFile(result.image_path))

    async def send_message(self, chat_id: str, text: str) -> None:
        """Отправка текстового сообщения в чат."""
        await self.bot.send_message(chat_id=int(chat_id), text=text)

    async def send_image(self, chat_id: str, image_path: str) -> None:
        """Отправка изображения в чат."""
        await self.bot.send_photo(
            chat_id=int(chat_id),
            photo=FSInputFile(image_path),
        )

    async def close(self) -> None:
        """Закрытие сессии бота."""
        await self.bot.session.close()
