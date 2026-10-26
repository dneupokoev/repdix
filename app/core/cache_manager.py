# app/core/cache_manager.py
dv_file_version = '261026.01'
"""Менеджер кэша на Redis (история диалогов, схема БД)."""
import json
import logging
import redis.asyncio as redis

logger = logging.getLogger(__name__)


class CacheManager:
    def __init__(self, host="localhost", port=6379, db=0):
        self.client = redis.Redis(host=host, port=port, db=db, decode_responses=True)

    async def get(self, key: str) -> str | None:
        try:
            return await self.client.get(key)
        except Exception as e:
            logger.warning(f"Redis get error: {e}")
            return None

    async def set(self, key: str, value: str, ttl: int = 86400) -> None:
        try:
            await self.client.set(key, value, ex=ttl)
        except Exception as e:
            logger.warning(f"Redis set error: {e}")

    async def delete(self, key: str) -> None:
        try:
            await self.client.delete(key)
        except Exception as e:
            logger.warning(f"Redis delete error: {e}")

    async def close(self) -> None:
        await self.client.close()