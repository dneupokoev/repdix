# app/interfaces/telegram_adapter.py
dv_file_version = '261026.10'
"""Telegram-адаптер на aiogram 3."""
import asyncio
import logging
import os

import yaml
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
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

        env_allowed_raw = os.environ.get("ALLOWED_USERS", "")
        self._env_allowed_users = {
            int(uid) for uid in env_allowed_raw.split(",") if uid.strip()
        }
        self.allowed_users = set(self._env_allowed_users)

        self._load_users_yaml()

        if not self.allowed_users:
            logging.getLogger(__name__).warning(
                "ALLOWED_USERS пуст и users.yaml отсутствует — доступ будет запрещён всем. "
                "Настройте ALLOWED_USERS или config/users.yaml."
            )

    def _load_users_yaml(self) -> None:
        """Загружает список разрешённых пользователей из config/users.yaml."""
        self.allowed_users = set(self._env_allowed_users)

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
        except Exception as e:
            logging.getLogger(__name__).warning("Failed to load users.yaml: %s", e)
            self.users_yaml_mtime = None

    async def start(self) -> None:
        """Регистрация хэндлеров и запуск polling."""
        self.dp.message.register(self.handle_start, Command("start"))
        self.dp.message.register(self.handle_message, F.text)
        await self.dp.start_polling(self.bot)

    async def handle_start(self, message: types.Message) -> None:
        """Приветствие по команде /start."""
        if message.from_user.id not in self.allowed_users:
            await message.answer("Доступ запрещён")
            return
        await message.answer(
            "Привет! Я repdix — помощник по базам данных.\n"
            "Отправьте /help для списка команд."
        )

    async def handle_message(self, message: types.Message) -> None:
        """Обработка входящего текстового сообщения."""
        if os.path.exists(self.users_yaml_path):
            current_mtime = os.path.getmtime(self.users_yaml_path)
            if self.users_yaml_mtime is None or current_mtime > self.users_yaml_mtime:
                self._load_users_yaml()

        if message.from_user.id not in self.allowed_users:
            await message.answer("Доступ запрещён")
            return

        async def send_status(text: str):
            try:
                await message.answer(text)
            except Exception as e:
                logging.getLogger(__name__).warning("Failed to send status: %s", e)

        msg = ChatMessage(
            user_id=str(message.from_user.id),
            chat_id=str(message.chat.id),
            text=message.text or "",
            status_callback=send_status
        )

        # Получаем состояние пользователя, чтобы сохранить задачу
        state = self.engine.sessions.get(str(message.from_user.id))

        async def run_and_reply():
            try:
                result: ChatResponse = await self.engine.process_message(msg)
                await self._send_long_message(message, result.text)
            except asyncio.CancelledError:
                await message.answer("⛔ Выполнение прервано командой /stop.")
            except Exception as e:
                logging.getLogger(__name__).error("Error in background task: %s", e)
                await message.answer("⚠️ Произошла ошибка при выполнении запроса.")

        # Если уже есть активная задача — не запускаем новую
        if state.current_task and not state.current_task.done():
            await message.answer("⏳ Пожалуйста, дождитесь ответа или отправьте /stop для отмены.")
            return

        # Запускаем задачу в фоне и сохраняем ссылку
        task = asyncio.create_task(run_and_reply())
        state.current_task = task

    async def _send_long_message(self, message: types.Message, text: str) -> None:
        """Отправляет сообщение, разбивая его на части, если оно длиннее 4096 символов."""
        max_length = 4000
        if len(text) <= max_length:
            await message.answer(text)
            return

        # Разбиваем по 4000 символов, стараясь резать по переносам строк
        parts = []
        while len(text) > max_length:
            split_idx = text.rfind('\n', 0, max_length)
            if split_idx == -1:
                split_idx = max_length
            parts.append(text[:split_idx])
            text = text[split_idx:].lstrip()

        if text:
            parts.append(text)

        for i, part in enumerate(parts):
            await message.answer(part)
            if i < len(parts) - 1:
                await asyncio.sleep(0.5) # Небольшая пауза, чтобы не получить Flood Control

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
