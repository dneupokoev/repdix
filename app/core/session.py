# app/core/session.py
dv_file_version = '261030.02'
"""Состояние сессии пользователя для repdix."""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# TTL сессии: если к ней не обращались столько секунд — удаляем.
# По умолчанию 24 часа (как TTL истории в Redis).
_DEFAULT_TTL_SEC = int(os.environ.get("SESSION_TTL_SEC") or str(24 * 3600))


@dataclass
class SessionState:
    """Состояние одного пользователя: история, выбранная БД/модель, lock.

    Поля:
        user_id: Telegram/CLI ID.
        history: История диалога (user/assistant/system).
        current_db: Выбранная БД (имя из databases.yaml).
        current_model: Явно выбранная модель (None = авто-роутер).
        last_model_used: Для отображения в /settings и аудита.
        lock: asyncio.Lock на пользователя (один запрос за раз).
        current_task: Ссылка на фоновую задачу (для /stop).
        last_sqls: Последние выполненные SQL (для контекста, не в history).
        summary: Краткое резюме старого диалога (auto-compact).
        long_term_facts: Задел на долговременную память (пока не используется).
        last_access: time.monotonic() последнего обращения (для TTL).
    """

    user_id: str
    history: list[dict] = field(default_factory=list)
    current_db: str | None = None
    current_model: str | None = None
    last_model_used: str = "none (deterministic)"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_task: asyncio.Task | None = None
    last_sqls: list[str] = field(default_factory=list)
    summary: str = ""
    long_term_facts: list[str] = field(default_factory=list)
    last_access: float = field(default_factory=time.monotonic)


class SessionStore:
    """Хранилище состояний пользователей с TTL и ленивой очисткой."""

    def __init__(
        self,
        default_db: str | None = None,
        ttl_sec: int = _DEFAULT_TTL_SEC,
    ) -> None:
        self._states: dict[str, SessionState] = {}
        self._default_db = default_db
        self._ttl_sec = max(int(ttl_sec), 60)  # не меньше минуты
        # Счётчик обращений — чтобы cleanup() запускался не на каждый get()
        self._access_counter = 0
        self._cleanup_every = 100
        # Фоновая очистка TTL (start_cleanup_task/stop_cleanup_task).
        self._cleanup_task: asyncio.Task | None = None
        self._cleanup_interval_sec = 300  # 5 минут

    # ------------------------------------------------------------------
    # Получение / создание
    # ------------------------------------------------------------------
    def get(self, user_id: str) -> SessionState:
        """Возвращает состояние пользователя, создавая при необходимости.

        Попутно обновляет last_access и лениво чистит просроченные сессии.
        """
        uid = str(user_id)
        now = time.monotonic()

        # Ленивая очистка (раз в _cleanup_every обращений)
        self._access_counter += 1
        if self._access_counter >= self._cleanup_every:
            self._access_counter = 0
            self._cleanup_expired(now)

        state = self._states.get(uid)
        if state is None:
            state = SessionState(user_id=uid, current_db=self._default_db)
            state.last_access = now
            self._states[uid] = state
        else:
            state.last_access = now
        return state

    def peek(self, user_id: str) -> SessionState | None:
        """Возвращает состояние без создания и без обновления last_access.

        Полезно для /stop: не хотим создавать сессию ради проверки.
        """
        return self._states.get(str(user_id))

    def touch(self, user_id: str) -> None:
        """Обновляет last_access (для длинных операций, чтобы TTL не сработал)."""
        state = self._states.get(str(user_id))
        if state is not None:
            state.last_access = time.monotonic()

    # ------------------------------------------------------------------
    # Очистка
    # ------------------------------------------------------------------
    def clear(self, user_id: str) -> None:
        """Удаляет состояние пользователя (например, по /clear).

        Если у сессии есть активная задача — отменяем её, чтобы не оставить
        висящий таск без ссылки.
        """
        uid = str(user_id)
        state = self._states.pop(uid, None)
        if state is None:
            return
        if state.current_task and not state.current_task.done():
            try:
                state.current_task.cancel()
                logger.info("Сессия %s очищена, текущая задача отменена", uid)
            except Exception as e:
                logger.warning("Не удалось отменить задачу при clear(%s): %s", uid, e)

    def clear_all(self) -> None:
        """Полная очистка (graceful shutdown, тесты)."""
        for uid in list(self._states.keys()):
            self.clear(uid)

    def _cleanup_expired(self, now: float) -> int:
        """Удаляет сессии, к которым не обращались дольше TTL.

        Returns:
            Количество удалённых сессий.
        """
        if not self._states:
            return 0

        expired: list[str] = []
        for uid, state in self._states.items():
            # Не трогаем сессии с активной задачей — иначе /stop не сработает
            if state.current_task and not state.current_task.done():
                continue
            if (now - state.last_access) > self._ttl_sec:
                expired.append(uid)

        for uid in expired:
            state = self._states.pop(uid, None)
            if state and state.current_task and not state.current_task.done():
                try:
                    state.current_task.cancel()
                except Exception:
                    pass

        if expired:
            logger.info("TTL: удалено %d просроченных сессий", len(expired))
        return len(expired)

    # ------------------------------------------------------------------
    # Фоновая очистка TTL
    # ------------------------------------------------------------------
    def start_cleanup_task(self) -> None:
        """Запускает фоновую очистку просроченных сессий.

        Идемпотентен: повторный вызов ничего не делает.
        Вызывать из main.py после создания engine.
        """
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info(
            "SessionStore: фоновая очистка запущена (интервал %d сек)",
            self._cleanup_interval_sec,
        )

    async def _cleanup_loop(self) -> None:
        """Фоновая очистка TTL. Не падает при ошибках."""
        try:
            while True:
                await asyncio.sleep(self._cleanup_interval_sec)
                try:
                    self._cleanup_expired(time.monotonic())
                except Exception as e:
                    logger.warning("SessionStore: ошибка cleanup: %s", e)
        except asyncio.CancelledError:
            logger.info("SessionStore: фоновая очистка остановлена")
            raise

    async def stop_cleanup_task(self) -> None:
        """Останавливает фоновую очистку (для graceful shutdown)."""
        if self._cleanup_task is None or self._cleanup_task.done():
            return
        self._cleanup_task.cancel()
        try:
            await self._cleanup_task
        except asyncio.CancelledError:
            pass
        self._cleanup_task = None

    # ------------------------------------------------------------------
    # Диагностика
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        """Возвращает статистику по хранилищу (для /settings и отладки)."""
        now = time.monotonic()
        active_tasks = sum(
            1 for s in self._states.values()
            if s.current_task and not s.current_task.done()
        )
        return {
            "sessions": len(self._states),
            "active_tasks": active_tasks,
            "ttl_sec": self._ttl_sec,
            "oldest_age_sec": (
                round(now - min(s.last_access for s in self._states.values()), 1)
                if self._states else 0
            ),
        }
