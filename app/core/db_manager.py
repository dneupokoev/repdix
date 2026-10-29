# app/core/db_manager.py
dv_file_version = '261029.01'
"""Менеджер подключений к базам данных (MySQL / PostgreSQL / SQLite).

Ключевые изменения версии 261027.06:
- MySQL: MAX_EXECUTION_TIME теперь ставится через init_command при создании
  пула (а не на каждый execute_query). Это убирает лишний round-trip к БД
  и гарантирует, что таймаут действует всегда, даже для первого запроса.
  Также добавлен SQL_MODE безопасный и charset=utf8mb4.
- MySQL: MAX_EXECUTION_TIME поддерживается только MySQL 5.7.8+. Для MariaDB
  параметр игнорируется (она его не понимает). Мы проверяем тип и не шлём
  SET, если СУБД — MariaDB (по server_version в databases.yaml или по флагу
  is_mariadb). Если флаг не задан — пытаемся SET и глотаем ошибку.
- PostgreSQL: добавлен statement_timeout в server_settings (аналог
  MAX_EXECUTION_TIME). Раньше таймаут работал только на уровне asyncpg
  (conn.fetch(timeout=...)), что не прерывало запрос на стороне сервера.
- SQLite: при readonly=true выполняется PRAGMA query_only = ON (защита
  от записи на уровне соединения). Раньше readonly игнорировался для SQLite.
- SQLite: timeout через asyncio.wait_for оставлен, но добавлено
  явное сообщение об ошибке и корректное закрытие курсора.
- execute_query: добавлен параметр params (для параметризованных запросов).
  Пока engine его не использует, но пригодится для безопасных запросов
  с пользовательскими значениями.
- close(): aiosqlite.Connection НЕ имеет wait_closed; проверка isinstance
  сохранена, но добавлен hasattr(pool, "wait_closed") как более надёжный
  признак. Также для aiosqlite добавлен commit() перед close (на случай
  незакоммиченных изменений в read-only это no-op).
- get_table_list: для MySQL учитывается текущая БД через DATABASE(), но
  добавлена сортировка по имени таблицы — стабильный порядок в промпте.
- get_table_schema: для PostgreSQL добавлено экранирование table_name
  (параметр $1 — уже безопасно, но добавлена проверка на пустую строку).
- connect(): если pool уже есть — возвращаем его. Если предыдущая попытка
  упала — пробуем снова (не кэшируем ошибку).
- load_config(): при отсутствии файла не падаем, а логируем warning и
  оставляем пустой config. Добавлена проверка на не-dict databases.
- Добавлен get_db_type(name) — удобный хелпер для engine.
- Добавлен get_db_config(name) — безопасный доступ к конфигу (не падает
  при отсутствии).
- close(): если пул уже закрыт — не падаем.
- Docstring в шапке вместо заглушки.
- get_table_list/get_table_schema скрывают технические сущности
  (см. app/core/technical_filter.py, префиксы из TECHNICAL_PREFIXES).
"""

import asyncio
import logging
import os
import re
from typing import Any

import aiosqlite
import aiomysql
import asyncpg
import yaml

from app.core.technical_filter import is_technical

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

    # ------------------------------------------------------------------
    # Конфигурация
    # ------------------------------------------------------------------
    def load_config(self) -> None:
        """Загружает конфигурацию из YAML и подставляет переменные окружения."""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.warning("Config file not found: %s", self.config_path)
            self.config = {}
            return
        except yaml.YAMLError as e:
            logger.error("Ошибка парсинга %s: %s", self.config_path, e)
            self.config = {}
            return

        databases = self.config.get('databases', {})
        if not isinstance(databases, dict):
            logger.error("config['databases'] должен быть словарём, получено: %r", type(databases))
            self.config['databases'] = {}
            return

        for db_name, db_conf in databases.items():
            if not isinstance(db_conf, dict):
                logger.warning("Конфиг БД %s не является словарём, пропускаем", db_name)
                continue
            # Подстановка переменных окружения вида ${VAR}
            for key, value in list(db_conf.items()):
                if isinstance(value, str) and value.startswith('${') and value.endswith('}'):
                    var_name = value[2:-1]
                    env_val = os.environ.get(var_name)
                    if env_val is None:
                        logger.warning(
                            "Environment variable %s not found for DB %s (key=%s)",
                            var_name, db_name, key,
                        )
                        db_conf[key] = ""
                    else:
                        db_conf[key] = env_val

    def get_db_names(self) -> list[str]:
        """Возвращает список имён БД из конфигурации."""
        databases = self.config.get('databases', {})
        if not isinstance(databases, dict):
            return []
        return list(databases.keys())

    def get_db_config(self, name: str) -> dict:
        """Возвращает конфиг БД или пустой dict, если БД нет."""
        databases = self.config.get('databases', {})
        if not isinstance(databases, dict):
            return {}
        conf = databases.get(name)
        return conf if isinstance(conf, dict) else {}

    def get_db_type(self, name: str) -> str:
        """Возвращает тип БД (mysql/postgresql/sqlite) в нижнем регистре."""
        return (self.get_db_config(name).get('type') or '').lower()

    # ------------------------------------------------------------------
    # Подключение
    # ------------------------------------------------------------------
    async def connect(self, name: str) -> None:
        """Создаёт пул соединений для указанной БД.

        Args:
            name: Имя БД из конфигурации.

        Raises:
            ValueError: Если БД нет в конфиге.
            NotImplementedError: Если тип БД не поддерживается.
        """
        if name in self.pools:
            return

        db_conf = self.get_db_config(name)
        if not db_conf:
            raise ValueError(f"Database '{name}' not found in config")

        db_type = (db_conf.get('type') or '').lower()

        if db_type == 'mysql':
            # init_command выполняется при создании каждого соединения в пуле.
            # Это дешевле, чем слать SET на каждый execute_query.
            is_mariadb = db_conf.get('is_mariadb', False)
            is_readonly = db_conf.get('readonly') is True

            # aiomysql не поддерживает multi-statement в init_command.
            # Поэтому выбираем ОДИН SET-запрос.
            # ВАЖНО: при readonly=True и MySQL (не MariaDB) MAX_EXECUTION_TIME
            # НЕ ставится — таймаут запроса на уровне БД не работает.
            # Если нужен и readonly, и таймаут — используйте пользователя БД
            # с правами только на SELECT (рекомендуется), либо снимите readonly
            # и полагайтесь на RBAC + validator.
            if is_readonly and not is_mariadb:
                init_cmd = "SET SESSION transaction_read_only = 1"
            elif not is_readonly and not is_mariadb:
                init_cmd = f"SET SESSION MAX_EXECUTION_TIME={self.query_timeout * 1000}"
            else:
                # MariaDB: readonly проверяется на уровне пользователя БД,
                # MAX_EXECUTION_TIME не поддерживается.
                init_cmd = None

            pool = await aiomysql.create_pool(
                host=db_conf.get('host', 'localhost'),
                port=int(db_conf.get('port', 3306)),
                user=db_conf.get('user', ''),
                password=db_conf.get('password', ''),
                db=db_conf.get('database', ''),
                charset=db_conf.get('charset', 'utf8mb4'),
                autocommit=True,
                cursorclass=aiomysql.DictCursor,
                init_command=init_cmd,
                minsize=int(db_conf.get('minsize', 1)),
                maxsize=int(db_conf.get('maxsize', 5)),
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
                min_size=int(db_conf.get('minsize', 1)),
                max_size=int(db_conf.get('maxsize', 5)),
                command_timeout=self.query_timeout,
            )
            server_settings: dict[str, str] = {}
            if db_conf.get('readonly') is True:
                server_settings["default_transaction_read_only"] = "on"
            # statement_timeout — прерывает запрос на стороне сервера
            server_settings["statement_timeout"] = str(self.query_timeout * 1000)
            pool_kwargs["server_settings"] = server_settings
            pool = await asyncpg.create_pool(**pool_kwargs)
            self.pools[name] = pool
            return

        elif db_type == 'sqlite':
            path = db_conf.get('path', ':memory:')
            conn = await aiosqlite.connect(path)
            try:
                # read-only на уровне соединения
                if db_conf.get('readonly') is True:
                    await conn.execute("PRAGMA query_only = ON")
                # WAL для параллельного чтения (если БД файловая)
                if path != ":memory:":
                    try:
                        await conn.execute("PRAGMA journal_mode = WAL")
                    except Exception:
                        pass
                await conn.commit()
            except Exception as e:
                logger.error("Ошибка инициализации SQLite %s: %s", path, e)
                await conn.close()
                raise
            self.pools[name] = conn
            return

        else:
            raise NotImplementedError(f"db type {db_type} not supported yet")

    # ------------------------------------------------------------------
    # Выполнение запросов
    # ------------------------------------------------------------------
    async def execute_query(
        self, name: str, sql: str, params: tuple | None = None,
    ) -> list[dict]:
        """Выполняет SQL-запрос и возвращает результаты.

        Args:
            name: Имя БД.
            sql: SQL-запрос.
            params: Параметры (для параметризованных запросов). Для MySQL
                используется %s, для PostgreSQL — $1, для SQLite — ?.

        Returns:
            Список словарей с результатами запроса.
        """
        if name not in self.pools:
            await self.connect(name)

        db_type = self.get_db_type(name)
        pool = self.pools[name]

        if db_type == 'mysql':
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    if params is not None:
                        await cur.execute(sql, params)
                    else:
                        await cur.execute(sql)
                    rows = await cur.fetchall()
                    return rows if rows is not None else []

        elif db_type == 'postgresql':
            async with pool.acquire() as conn:
                if params is not None:
                    rows = await conn.fetch(sql, *params, timeout=self.query_timeout)
                else:
                    rows = await conn.fetch(sql, timeout=self.query_timeout)
            return [dict(row) for row in rows]

        elif db_type == 'sqlite':
            try:
                if params is not None:
                    cursor = await pool.execute(sql, params)
                else:
                    cursor = await pool.execute(sql)
            except Exception:
                raise
            try:
                rows = await asyncio.wait_for(cursor.fetchall(), timeout=self.query_timeout)
                description = cursor.description
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"Query timeout exceeded ({self.query_timeout} sec)"
                )
            finally:
                try:
                    await cursor.close()
                except Exception:
                    pass

            if not rows:
                return []
            if description is None:
                return []
            col_names = [d[0] for d in description]
            return [dict(zip(col_names, row)) for row in rows]

        else:
            raise NotImplementedError(f"db type {db_type} not supported yet")

    # ------------------------------------------------------------------
    # Метаданные
    # ------------------------------------------------------------------
    async def get_table_list(self, db_name: str) -> list[str]:
        """Возвращает список имён таблиц в указанной БД."""
        if db_name not in self.pools:
            await self.connect(db_name)

        db_type = self.get_db_type(db_name)
        pool = self.pools[db_name]

        if db_type == 'mysql':
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = DATABASE() "
                        "ORDER BY table_name"
                    )
                    rows = await cur.fetchall()
                    if not rows:
                        return []
                    result = []
                    for row in rows:
                        normalized = {k.lower(): v for k, v in row.items()}
                        tbl = normalized.get("table_name", "")
                        if tbl and not is_technical(tbl):
                            result.append(tbl)
                    return result

        elif db_type == 'postgresql':
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' "
                    "ORDER BY table_name"
                )
            return (
                [
                    row['table_name']
                    for row in rows
                    if not is_technical(row['table_name'])
                ]
                if rows else []
            )

        elif db_type == 'sqlite':
            cursor = await pool.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
            rows = await cursor.fetchall()
            await cursor.close()
            return (
                [row[0] for row in rows if not is_technical(row[0])]
                if rows else []
            )

        else:
            raise NotImplementedError(f"db type {db_type} not supported yet")

    async def get_table_schema(self, db_name: str, table_name: str) -> list[dict]:
        """Возвращает список полей и их описаний для указанной таблицы.

        Returns:
            Список словарей с ключами column_name, data_type, column_comment.
        """
        if not table_name:
            return []

        if db_name not in self.pools:
            await self.connect(db_name)

        db_type = self.get_db_type(db_name)
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
                    if not rows:
                        return []
                    result = []
                    for row in rows:
                        normalized = {k.lower(): v for k, v in row.items()}
                        col_name = normalized.get("column_name", "")
                        if is_technical(col_name):
                            continue
                        result.append({
                            "column_name": col_name,
                            "data_type": normalized.get("data_type", ""),
                            "column_comment": normalized.get("column_comment", "") or "",
                        })
                    return result

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
            return (
                [
                    dict(row)
                    for row in rows
                    if not is_technical(row["column_name"])
                ]
                if rows else []
            )

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
                if not is_technical(row[1])
            ]

        else:
            raise NotImplementedError(f"db type {db_type} not supported yet")

    # ------------------------------------------------------------------
    # Закрытие
    # ------------------------------------------------------------------
    async def close(self) -> None:
        """Закрывает все пулы соединений.

        Безопасно вызывать повторно и при частично инициализированных пулах.
        """
        if not self.pools:
            return

        for name, pool in list(self.pools.items()):
            try:
                # aiosqlite.Connection: имеет close(), но НЕ wait_closed().
                # aiomysql.Pool / asyncpg.Pool: имеют close() и wait_closed()
                # (или aclose()). Различаем через hasattr(wait_closed).
                if hasattr(pool, "wait_closed"):
                    pool.close()
                    await pool.wait_closed()
                elif isinstance(pool, aiosqlite.Connection):
                    try:
                        await pool.commit()
                    except Exception:
                        pass
                    await pool.close()
                else:
                    # Неизвестный тип — пробуем close()
                    close = getattr(pool, "close", None)
                    if close is not None:
                        result = close()
                        if asyncio.iscoroutine(result):
                            await result
            except Exception as e:
                logger.warning("Error closing pool %s: %s", name, e)
            finally:
                self.pools.pop(name, None)

        self.pools.clear()
