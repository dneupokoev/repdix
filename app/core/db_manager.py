# app/core/db_manager.py
dv_file_version = '261026.11'
"""Менеджер подключений к базам данных."""
import asyncio
import logging
import os
import re
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
        self.query_timeout = int(os.environ.get("SQL_QUERY_TIMEOUT") or "15")
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
            init_cmd = "SET SESSION TRANSACTION READ ONLY" if db_conf.get('readonly') is True else None
            pool = await aiomysql.create_pool(
                host=db_conf.get('host', 'localhost'),
                port=int(db_conf.get('port', 3306)),
                user=db_conf.get('user', ''),
                password=db_conf.get('password', ''),
                db=db_conf.get('database', ''),
                autocommit=True,
                cursorclass=aiomysql.DictCursor,
                init_command=init_cmd,
            )
            self.pools[name] = pool
            return
        elif db_type == 'postgresql':
            pool_kwargs = dict(
                host=db_conf.get('host', 'localhost'),
                port=int(db_conf.get('port', 5432)),
                user=db_conf.get('user', 'postgres'),
                password=db_conf.get('password', ''),
                database=db_conf.get('database', 'postgres'),
                min_size=1,
                max_size=5,
                command_timeout=self.query_timeout,
            )
            if db_conf.get('readonly') is True:
                pool_kwargs["server_settings"] = {"default_transaction_read_only": "on"}
            pool = await asyncpg.create_pool(**pool_kwargs)
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
                    # Устанавливаем таймаут в миллисекундах
                    await cur.execute(f"SET SESSION MAX_EXECUTION_TIME={self.query_timeout * 1000}")
                    await cur.execute(sql)
                    rows = await cur.fetchall()
                    return rows if rows is not None else []

        elif db_type == 'postgresql':
            async with pool.acquire() as conn:
                # asyncpg поддерживает timeout в fetch
                rows = await conn.fetch(sql, timeout=self.query_timeout)
            return [dict(row) for row in rows]

        elif db_type == 'sqlite':
            cursor = await pool.execute(sql)
            try:
                rows = await asyncio.wait_for(cursor.fetchall(), timeout=self.query_timeout)
                description = cursor.description
            except asyncio.TimeoutError:
                raise TimeoutError(f"Query timeout exceeded ({self.query_timeout} sec)")
            finally:
                await cursor.close()

            if not rows:
                return []
            if description is None:
                return []
            col_names = [d[0] for d in description]
            return [dict(zip(col_names, row)) for row in rows]

        else:
            raise NotImplementedError(f"db type {db_type} not supported yet")

    async def get_table_list(self, db_name: str) -> list[str]:
        """Возвращает список имён таблиц в указанной БД.

        Args:
            db_name: Имя БД.

        Returns:
            Список имён таблиц.
        """
        if db_name not in self.pools:
            await self.connect(db_name)

        db_type = self.config.get('databases', {}).get(db_name, {}).get('type', '').lower()
        pool = self.pools[db_name]

        if db_type == 'mysql':
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()"
                    )
                    rows = await cur.fetchall()
                    return [row['table_name'] for row in rows] if rows else []

        elif db_type == 'postgresql':
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            return [row['table_name'] for row in rows] if rows else []

        elif db_type == 'sqlite':
            cursor = await pool.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            rows = await cursor.fetchall()
            await cursor.close()
            return [row[0] for row in rows] if rows else []

        else:
            raise NotImplementedError(f"db type {db_type} not supported yet")

    async def get_table_schema(self, db_name: str, table_name: str) -> list[dict]:
        """Возвращает список полей и их описаний для указанной таблицы.

        Args:
            db_name: Имя БД.
            table_name: Имя таблицы.

        Returns:
            Список словарей с ключами column_name, data_type, column_comment.
        """
        if db_name not in self.pools:
            await self.connect(db_name)

        db_type = self.config.get('databases', {}).get(db_name, {}).get('type', '').lower()
        pool = self.pools[db_name]

        if db_type == 'mysql':
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT column_name, data_type, column_comment "
                        "FROM information_schema.columns "
                        "WHERE table_schema = DATABASE() AND table_name = %s "
                        "ORDER BY ordinal_position",
                        (table_name,)
                    )
                    rows = await cur.fetchall()
                    if rows:
                        return rows
                    # Если таблица не найдена — попробуем найти похожие
                    await cur.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = DATABASE() AND table_name LIKE %s LIMIT 5",
                        (f"%{table_name}%",)
                    )
                    similar = await cur.fetchall()
                    if similar:
                        return [
                            {"column_name": "(таблица не найдена)",
                             "data_type": "",
                             "column_comment": f"Похожие таблицы: " + ", ".join(r['table_name'] for r in similar)}
                        ]
                    return []

        elif db_type == 'postgresql':
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT c.column_name, c.data_type, "
                    "       COALESCE(d.description, '') AS column_comment "
                    "FROM information_schema.columns c "
                    "LEFT JOIN pg_catalog.pg_statio_all_tables st "
                    "  ON st.schemaname = c.table_schema AND st.relname = c.table_name "
                    "LEFT JOIN pg_catalog.pg_description d "
                    "  ON d.objoid = st.relid AND d.objsubid = c.ordinal_position "
                    "WHERE c.table_schema = 'public' AND c.table_name = $1 "
                    "ORDER BY c.ordinal_position",
                    table_name
                )
            return [dict(row) for row in rows] if rows else []

        elif db_type == 'sqlite':
            if not re.fullmatch(r'[A-Za-z0-9_]+', table_name):
                logger.warning("Invalid table_name for SQLite PRAGMA: %s", table_name)
                return []
            cursor = await pool.execute(f"PRAGMA table_info({table_name})")
            rows = await cursor.fetchall()
            await cursor.close()
            if not rows:
                return []
            return [
                {"column_name": row[1], "data_type": row[2], "column_comment": ""}
                for row in rows
            ]

        else:
            raise NotImplementedError(f"db type {db_type} not supported yet")

    async def close(self) -> None:
        """Закрывает все пулы соединений."""
        for pool in self.pools.values():
            try:
                if isinstance(pool, aiosqlite.Connection):
                    await pool.close()
                else:
                    pool.close()
                    await pool.wait_closed()
            except Exception as e:
                logger.warning("Error closing pool: %s", e)
        self.pools.clear()
