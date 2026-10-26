# app/core/session.py
dv_file_version = '261026.02'
"""Состояние сессии пользователя для repdix."""
import asyncio
import weakref
from dataclasses import dataclass, field


@dataclass
class SessionState:
    """Состояние одного пользователя: история, выбранная БД/модель, lock."""

    user_id: str
    history: list[dict] = field(default_factory=list)
    current_db: str | None = None
    current_model: str | None = None
    last_model_used: str = "none (deterministic)"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_task: asyncio.Task | None = None


class SessionStore:
    """Хранилище состояний пользователей с автоматической очисткой."""

    def __init__(self, default_db: str | None = None) -> None:
        self._states: "weakref.WeakValueDictionary[str, SessionState]" = (
            weakref.WeakValueDictionary()
        )
        self._default_db = default_db

    def get(self, user_id: str) -> SessionState:
        """Возвращает состояние пользователя, создавая при необходимости."""
        state = self._states.get(user_id)
        if state is None:
            state = SessionState(user_id=user_id, current_db=self._default_db)
            self._states[user_id] = state
        return state

    def clear(self, user_id: str) -> None:
        """Удаляет состояние пользователя (например, по /clear)."""
        self._states.pop(user_id, None)
