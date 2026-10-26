# app/interfaces/base.py
dv_file_version = '261025.01'
"""Базовые интерфейсы и DTO для адаптеров мессенджеров."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class ChatMessage:
    """Входящее сообщение от пользователя."""

    user_id: str
    chat_id: str
    text: str


@dataclass
class ChatResponse:
    """Ответ ядра для отправки пользователю."""

    text: str
    image_path: Optional[str] = None


class BaseChatInterface(ABC):
    """Абстрактный интерфейс адаптера мессенджера."""

    @abstractmethod
    async def start(self) -> None:
        """Запуск адаптера (подключение, цикл событий)."""

    @abstractmethod
    async def send_message(self, chat_id: str, text: str) -> None:
        """Отправка текстового сообщения в чат."""

    @abstractmethod
    async def send_image(self, chat_id: str, image_path: str) -> None:
        """Отправка изображения в чат."""
