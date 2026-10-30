# app/core/cache_manager.py
dv_file_version = '261030.01'
"""Менеджер кэша на Redis (история диалогов, схема БД)."""

import logging

import redis.asyncio as redis

logger = logging.getLogger(__name__)


class CacheManager:
    """Обёртка над redis.asyncio.Redis с graceful degradation.

    Все методы (кроме ping/healthcheck) глотают ошибки Redis и возвращают
    безопасные значения — бот продолжает работать без кэша.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: str | None = None,
        socket_timeout: float = 5.0,
        socket_connect_timeout: float = 5.0,
    ) -> None:
        """Инициализация клиента Redis.

        Args:
            host: Хост Redis.
            port: Порт.
            db: Номер БД (по умолчанию 0).
            password: Пароль (None или "" = без пароля).
            socket_timeout: Таймаут чтения/записи (сек).
            socket_connect_timeout: Таймаут подключения (сек).

        Raises:
            ValueError: Redis на не-loopback хосте настроен без пароля.
        """
        # Fail-fast: открытый в сеть Redis без пароля — известный вектор
        # компрометации (CVE-2025-21605). Разрешаем только loopback.
        if not password and host not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError(
                "Redis без пароля на удалённом хосте запрещён (CVE-2025-21605)."
            )

        self.client = redis.Redis(
            host=host,
            port=port,
            db=db,
            password=password or None,
            decode_responses=True,
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_connect_timeout,
        )
        self._closed = False

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    async def ping(self) -> bool:
        """Проверяет доступность Redis.

        Returns:
            True, если PING успешен. False — если нет (с логом).

        Не бросает исключений — удобно для fail-fast в main.py.
        """
        try:
            await self.client.ping()
            return True
        except Exception as e:
            logger.warning("Redis ping error: %s", e)
            return False

    async def healthcheck(self) -> bool:
        """Алиас ping() для читаемости."""
        return await self.ping()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    async def get(self, key: str) -> str | None:
        """Возвращает значение по ключу или None."""
        try:
            return await self.client.get(key)
        except Exception as e:
            logger.warning("Redis get error (key=%s): %s", key, e)
            return None

    async def set(self, key: str, value: str, ttl: int = 86400) -> bool:
        """Сохраняет значение с TTL (по умолчанию 24 часа).

        Returns:
            True — сохранено; False — ошибка Redis.
        """
        if value is None:
            logger.warning("Redis set: value=None для ключа %s, пропускаем", key)
            return False
        try:
            await self.client.set(key, value, ex=ttl)
            return True
        except Exception as e:
            logger.warning("Redis set error (key=%s): %s", key, e)
            return False

    async def delete(self, key: str) -> bool:
        """Удаляет ключ.

        Returns:
            True — удалён (или не существовал); False — ошибка Redis.
        """
        try:
            await self.client.delete(key)
            return True
        except Exception as e:
            logger.warning("Redis delete error (key=%s): %s", key, e)
            return False

    # ------------------------------------------------------------------
    # Батч-операции
    # ------------------------------------------------------------------
    async def mget(self, keys: list[str]) -> list[str | None]:
        """Возвращает значения по списку ключей (None для отсутствующих)."""
        if not keys:
            return []
        try:
            return await self.client.mget(keys)
        except Exception as e:
            logger.warning("Redis mget error (keys=%d): %s", len(keys), e)
            return [None] * len(keys)

    async def mset(self, mapping: dict[str, str], ttl: int = 86400) -> bool:
        """Сохраняет несколько ключей с одинаковым TTL.

        Returns:
            True — сохранено; False — ошибка.
        """
        if not mapping:
            return True
        try:
            # pipeline: SET для каждого ключа с TTL
            async with self.client.pipeline(transaction=False) as pipe:
                for key, value in mapping.items():
                    if value is None:
                        continue
                    pipe.set(key, value, ex=ttl)
                await pipe.execute()
            return True
        except Exception as e:
            logger.warning("Redis mset error (keys=%d): %s", len(mapping), e)
            return False

    async def delete_pattern(self, pattern: str) -> int:
        """Удаляет все ключи, подходящие под pattern (через SCAN, не KEYS).

        Использует SCAN — безопасно для больших баз (KEYS блокирует Redis).

        Returns:
            Количество удалённых ключей. -1 — ошибка.
        """
        if not pattern:
            return 0
        deleted = 0
        try:
            async for key in self.client.scan_iter(match=pattern, count=100):
                await self.client.delete(key)
                deleted += 1
            if deleted:
                logger.info("Redis delete_pattern(%s): удалено %d ключей", pattern, deleted)
            return deleted
        except Exception as e:
            logger.warning("Redis delete_pattern error (pattern=%s): %s", pattern, e)
            return -1

    # ------------------------------------------------------------------
    # Закрытие
    # ------------------------------------------------------------------
    async def close(self) -> None:
        """Закрывает соединение с Redis.

        Идемпотентен: повторный вызов ничего не делает.
        Использует aclose() (redis-py >= 5.0), с fallback на close()
        для старых версий.
        """
        if self._closed:
            return
        self._closed = True

        # aclose() — актуальный метод в redis-py >= 5.0
        aclose = getattr(self.client, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
                logger.info("Redis: соединение закрыто (aclose)")
                return
            except Exception as e:
                logger.warning("Redis aclose error: %s", e)
                # не return — пробуем close() как fallback

        # Fallback для старых версий
        close = getattr(self.client, "close", None)
        if close is not None:
            try:
                result = close()
                # redis-py close() — async
                if hasattr(result, "__await__"):
                    await result
                logger.info("Redis: соединение закрыто (close)")
            except Exception as e:
                logger.warning("Redis close error: %s", e)
