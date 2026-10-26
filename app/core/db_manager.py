# app/core/db_manager.py
dv_file_version = '261026.01'
"""Менеджер подключений к базам данных."""
import logging
import os
from typing import Any

import yaml
import aiomysql
import asyncpg
import aiosqlite

logger = logging.getLogger(__name__)


class DBManager:
    """Управляет пулами соединений к базам данных."""

    def __init__(self, config_path: str = "config/databases.yaml") -> None:
        """Инициализация менеджера БД.

        Args:
            config_path: Путь к YAML-файлу с конфигурацией БД.
        """
        self.config_path = config_path
        self.pools: dict[str, Any] = {}
        self.config: dict = {}
        self.load_config()

    def load_config(self) -> None:
        """Загружает конфигурацию из YAML и подставляет переменные окружения."""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f)
        except FileNotFoundError:
            logger.warning("Config file not found: %s", self.config_path)
            self.config = {}
            return

        databases = self.config.get('databases', {})
        for db_name, db_conf in databases.items():
            # Подстановка переменных окружения вида ${VAR}
            for key, value in db_conf.items():
                if isinstance(value, str) and value.startswith('${') and value.endswith('}'):
                    var_name = value[2:-1]
                    env_val = os.environ.get(var_name)
                    if env_val is None:
                        logger.warning("Environment variable %s not found for DB %s", var_name, db_name)
                        db_conf[key] = ""
                    else:
                        db_conf[key] = env_val

    def get_db_names(self) -> list[str]:
        """Возвращает список имён БД из конфигурации.

        Returns:
            Список ключей из config['databases'].
        """
        return list(self.config.get('databases', {}).keys())

    async def connect(self, name: str) -> None:
        """Создает пул соединений для указанной БД.

        Args:
            name: Имя БД из конфигурации.
        """
        if name in self.pools:
            return

        databases = self.config.get('databases', {})
        if name not in databases:
            raise ValueError(f"Database '{name}' not found in config")

        db_conf = databases[name]
        db_type = db_conf.get('type', '').lower()

        if db_type == 'mysql':
            pool = await aiomysql.create_pool(
                host=db_conf.get('host', 'localhost'),
                port=int(db_conf.get('port', 3306)),
                user=db_conf.get('user', ''),
                password=db_conf.get('password', ''),
                db=db_conf.get('database', ''),
                autocommit=True,
                cursorclass=aiomysql.DictCursor
            )
        elif db_type == 'postgresql':
            pool = await asyncpg.create_pool(
                host=db_conf.get('host', 'localhost'),
                port=int(db_conf.get('port', 5432)),
                user=db_conf.get('user', 'postgres'),
                password=db_conf.get('password', ''),
                database=db_conf.get('database', 'postgres'),
                min_size=1,
                max_size=5,
            )
        elif db_type == 'sqlite':
            pool = await aiosqlite.connect(db_conf.get('path', ':memory:'))
        else:
            raise NotImplementedError(f"db type {db_type} not supported yet")

        self.pools[name] = pool

    async def execute_query(self, name: str, sql: str) -> list[dict]:
        """Выполняет SQL-запрос и возвращает результаты.

        Args:
            name: Имя БД.
            sql: SQL-запрос.

        Returns:
            Список словарей с результатами запроса.
        """
        if name not in self.pools:
            await self.connect(name)

        db_type = self.config.get('databases', {}).get(name, {}).get('type', '').lower()
        pool = self.pools[name]

        if db_type == 'mysql':
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql)
                    rows = await cur.fetchall()
                    return rows if rows is not None else []

        elif db_type == 'postgresql':
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql)
            return [dict(row) for row in rows]

        elif db_type == 'sqlite':
            cursor = await pool.execute(sql)
            rows = await cursor.fetchall()
            await cursor.close()
            if not rows:
                return []
            col_names = [description[0] for description in cursor.description]
            return [dict(zip(col_names, row)) for row in rows]

        else:
            raise NotImplementedError(f"db type {db_type} not supported yet")

    async def close(self) -> None:
        """Закрывает все пулы соединений."""
        for pool in self.pools.values():
            if isinstance(pool, aiosqlite.Connection):
                await pool.close()
            else:
                pool.close()
                await pool.wait_closed()
        self.pools.clear()
