# app/core/db_manager.py
dv_file_version = '261030.02'
"""Менеджер подключений к базам данных (MySQL / PostgreSQL / SQLite).

Ключевые изменения версии 261030.02:
- MySQL/MariaDB: в автогенерируемый init_command добавлены
  "SET SESSION innodb_lock_wait_timeout = <N>" и
  "SET SESSION lock_wait_timeout = <N>" (N = LOCK_WAIT_TIMEOUT из окружения,
  по умолчанию 5 секунд). MAX_EXECUTION_TIME/max_statement_time не запускают
  таймер, пока запрос ждёт блокировку, поэтому ожидание блокировок
  ограничивается отдельно, на уровне сессии. Работает для MySQL и MariaDB,
  в том числе при readonly=true. Явно заданный init_statements по-прежнему
  полностью переопределяет автогенерацию.

Ключевые изменения версии 261030.01:
- MySQL/MariaDB: при автогенерации init_statements серверный таймаут
  ставится ВСЕГДА, независимо от readonly. Для MariaDB это
  "SET SESSION max_statement_time = <SQL_QUERY_TIMEOUT>" (в секундах),
  для MySQL — "SET SESSION MAX_EXECUTION_TIME = <мс>". Ранее для
  MariaDB при readonly=false серверный таймаут не выставлялся вообще.
  transaction_read_only по-прежнему ставится только при readonly=true.
- Явно заданный непустой init_statements полностью переопределяет
  автогенерацию (без изменений).

Ключевые изменения версии 261029.04:
- MySQL: init_command теперь может содержать НЕСКОЛЬКО SET-запросов через
  "; " (пул создаётся с client_flag=CLIENT.MULTI_STATEMENTS). Благодаря
  этому при readonly=true всё равно ставится MAX_EXECUTION_TIME.
- MySQL: новый опциональный параметр конфига `init_statements` (список
  строк). Если он задан — используется он вместо автогенерируемых SET.
- MySQL/MariaDB: при readonly=true ставится
  "SET SESSION transaction_read_only = 1" + серверный таймаут
  (MAX_EXECUTION_TIME в мс для MySQL, max_statement_time в секундах для
  MariaDB). Для MariaDB без readonly серверный таймаут не ставится.

Ключевые изменения версии 261029.03:
- MySQL: execute_query обёрнут в asyncio.wait_for(timeout=SQL_QUERY_TIMEOUT+2).
  Клиентский таймаут — последняя линия защиты, если серверный таймаут
  (MAX_EXECUTION_TIME) не применился. При срабатывании выбрасывается
  TimeoutError с тем же текстом, что и для SQLite.

Ключевые изменения версии 261029.02:
- Поддержка нескольких схем в одной MySQL-БД: конфиг `schemas` (список или
  "auto"), `exclude_schemas`, `description`.
- get_schemas(), get_db_description(), get_db_display_name().
- get_table_list()/get_table_schema() принимают schema= и работают с
  fully-qualified именами (schema.table). ЭТО BREAKING CHANGE.
- In-memory кэш схемы/таблиц/колонок с TTL (SCHEMA_CACHE_TTL_SEC, 3600),
  invalidate_schema_cache(), очистка кэша в close().

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
import time
from typing import Any

import aiosqlite
import aiomysql
import asyncpg
import yaml
from pymysql.constants import CLIENT

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
        try:
            self.schema_cache_ttl = int(
                os.environ.get("SCHEMA_CACHE_TTL_SEC") or "3600"
            )
        except (TypeError, ValueError):
            self.schema_cache_ttl = 3600
        try:
            self.lock_wait_timeout = int(
                os.environ.get("LOCK_WAIT_TIMEOUT", "5")
            )
        except (TypeError, ValueError):
            logger.warning(
                "Некорректное значение LOCK_WAIT_TIMEOUT=%r, используем 5",
                os.environ.get("LOCK_WAIT_TIMEOUT"),
            )
            self.lock_wait_timeout = 5
        if self.lock_wait_timeout <= 0:
            logger.warning(
                "LOCK_WAIT_TIMEOUT=%s должен быть > 0, используем 5",
                self.lock_wait_timeout,
            )
            self.lock_wait_timeout = 5
        # key -> (monotonic_timestamp, data)
        self._schema_cache: dict[tuple, tuple[float, Any]] = {}
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

    def get_db_description(self, name: str) -> str:
        """Возвращает описание БД из конфига (или пустую строку)."""
        conf = self.get_db_config(name)
        return (conf.get("description") or "").strip()

    def get_db_display_name(self, name: str) -> str:
        """Возвращает "name — description" (или просто name без описания)."""
        desc = self.get_db_description(name)
        return f"{name} — {desc}" if desc else name

    # ------------------------------------------------------------------
    # Кэш метаданных (схемы/таблицы/колонки)
    # ------------------------------------------------------------------
    def _cache_get(self, key: tuple) -> Any | None:
        """Возвращает закэшированное значение или None, если нет/устарело."""
        entry = self._schema_cache.get(key)
        if entry is None:
            return None
        ts, data = entry
        if time.monotonic() - ts > self.schema_cache_ttl:
            self._schema_cache.pop(key, None)
            return None
        return data

    def _cache_set(self, key: tuple, data: Any) -> None:
        """Кладёт значение в кэш с текущим timestamp."""
        self._schema_cache[key] = (time.monotonic(), data)

    def invalidate_schema_cache(self, db_name: str | None = None) -> None:
        """Сбрасывает кэш метаданных.

        Args:
            db_name: None — очистить весь кэш; иначе удалить все ключи,
                в которых встречается это имя БД.
        """
        if db_name is None:
            self._schema_cache.clear()
            return
        for key in list(self._schema_cache.keys()):
            if db_name in key:
                self._schema_cache.pop(key, None)

    # ------------------------------------------------------------------
    # Схемы
    # ------------------------------------------------------------------
    async def get_schemas(self, db_name: str) -> list[str]:
        """Возвращает список схем (database) для указанной БД.

        Логика:
          - schemas: [..] — явный список (technical_* отфильтровываются);
          - schemas: "auto" — MySQL: опрос information_schema.schemata с
            вычитанием exclude_schemas и технических схем;
          - не задано — fallback [conf["database"]] (для SQLite — ["main"]).

        При ошибке SQL логируется warning и возвращается fallback.
        """
        cache_key = ("schemas", db_name)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        conf = self.get_db_config(db_name)
        db_type = (conf.get("type") or "").lower()
        fallback_db = (conf.get("database") or "").strip()

        def _fallback() -> list[str]:
            if db_type == "sqlite":
                return ["main"]
            if fallback_db and not is_technical(fallback_db):
                return [fallback_db]
            return []

        schemas_conf = conf.get("schemas")

        if isinstance(schemas_conf, str) and schemas_conf.strip().lower() == "auto":
            if db_type == "mysql":
                exclude_raw = conf.get("exclude_schemas")
                if not isinstance(exclude_raw, list):
                    exclude_raw = [
                        "mysql",
                        "information_schema",
                        "performance_schema",
                        "sys",
                    ]
                exclude_l = {str(e).lower() for e in exclude_raw}
                try:
                    rows = await self.execute_query(
                        db_name,
                        "SELECT schema_name FROM information_schema.schemata",
                    )
                    names: list[str] = []
                    for row in rows or []:
                        value = None
                        for k, v in row.items():
                            if str(k).lower() == "schema_name":
                                value = v
                                break
                        if value is None:
                            continue
                        s = str(value)
                        if s.lower() in exclude_l or is_technical(s):
                            continue
                        names.append(s)
                    names.sort()
                    self._cache_set(cache_key, names)
                    return names
                except Exception as e:
                    logger.warning(
                        "get_schemas(auto) failed for %s: %s", db_name, e
                    )
                    return _fallback()
            # postgresql/sqlite: auto вне текущей задачи
            result = _fallback()
            self._cache_set(cache_key, result)
            return result

        if isinstance(schemas_conf, list):
            result = [
                str(s)
                for s in schemas_conf
                if s and not is_technical(str(s))
            ]
            self._cache_set(cache_key, result)
            return result

        result = _fallback()
        self._cache_set(cache_key, result)
        return result

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
            # Multi-statement включается через client_flag ниже, поэтому
            # в init_command можно объединять несколько SET через "; ".
            is_mariadb = bool(db_conf.get('is_mariadb', False))
            is_readonly = db_conf.get('readonly') is True
            mysql_timeout_ms = self.query_timeout * 1000

            init_statements_conf = db_conf.get('init_statements')
            if init_statements_conf is not None and not isinstance(init_statements_conf, list):
                logger.warning(
                    "DB %s: init_statements должен быть списком, получено %r — игнорируем",
                    name, type(init_statements_conf),
                )
                init_statements_conf = None

            if init_statements_conf:
                init_stmts = [
                    str(stmt).strip() for stmt in init_statements_conf
                    if str(stmt).strip()
                ]
            else:
                # Автогенерируемые SET-запросы.
                # readonly → запрет записи на уровне сессии + серверный таймаут.
                # MariaDB не понимает MAX_EXECUTION_TIME — для неё
                # max_statement_time (в СЕКУНДАХ). max_statement_time не
                # требует прав на запись, поэтому ставим его всегда.
                init_stmts = []
                if is_readonly:
                    init_stmts.append("SET SESSION transaction_read_only = 1")
                if is_mariadb:
                    init_stmts.append(
                        f"SET SESSION max_statement_time = {self.query_timeout}"
                    )
                else:
                    init_stmts.append(
                        f"SET SESSION MAX_EXECUTION_TIME = {mysql_timeout_ms}"
                    )
                # Ограничение ожидания блокировок на уровне сессии:
                # MAX_EXECUTION_TIME/max_statement_time не тикают, пока
                # запрос ждёт блокировку. innodb_lock_wait_timeout — блокировки
                # InnoDB, lock_wait_timeout — ожидание метаданных (MDL).
                # Добавляем всегда: и для MySQL, и для MariaDB, и в readonly.
                init_stmts.append(
                    "SET SESSION innodb_lock_wait_timeout = "
                    f"{self.lock_wait_timeout}"
                )
                init_stmts.append(
                    f"SET SESSION lock_wait_timeout = {self.lock_wait_timeout}"
                )

            init_cmd = "; ".join(init_stmts) if init_stmts else None
            if init_cmd:
                logger.debug("DB %s: mysql init_command=%s", name, init_cmd)

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
                client_flag=CLIENT.MULTI_STATEMENTS,
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
            async def _run_mysql_query() -> list[dict]:
                async with pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        if params is not None:
                            await cur.execute(sql, params)
                        else:
                            await cur.execute(sql)
                        rows = await cur.fetchall()
                        return rows if rows is not None else []

            # Серверный таймаут (MAX_EXECUTION_TIME) ставится не всегда:
            # в режиме readonly=true он не применяется. asyncio.wait_for —
            # последняя линия защиты от «вечного» запроса.
            try:
                return await asyncio.wait_for(
                    _run_mysql_query(), timeout=self.query_timeout + 2,
                )
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"Query timeout exceeded ({self.query_timeout} sec)"
                )

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
    async def get_table_list(
        self, db_name: str, schema: str | None = None,
    ) -> list[str]:
        """Возвращает список таблиц в виде fully-qualified имён "schema.table".

        Args:
            db_name: Имя БД из конфигурации.
            schema: Конкретная схема. Если None — обходятся все схемы из
                get_schemas(db_name).

        Returns:
            Отсортированный список "schema.table" без технических таблиц.
        """
        cache_key = ("tables", db_name, schema or "*")
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        schemas = [schema] if schema is not None else await self.get_schemas(db_name)

        result: list[str] = []
        for sch in schemas:
            try:
                tables = await self._fetch_tables(db_name, sch)
            except NotImplementedError:
                raise
            except Exception as e:
                logger.warning(
                    "get_table_list(%s, %s) failed: %s", db_name, sch, e
                )
                continue
            for tbl in tables:
                if not tbl or is_technical(tbl):
                    continue
                result.append(f"{sch}.{tbl}" if sch else tbl)

        result = sorted(set(result))
        self._cache_set(cache_key, result)
        return result

    async def _fetch_tables(self, db_name: str, schema: str) -> list[str]:
        """Возвращает короткие имена таблиц одной схемы (без фильтрации)."""
        db_type = self.get_db_type(db_name)

        if db_type == 'mysql':
            rows = await self.execute_query(
                db_name,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s "
                "ORDER BY table_name",
                (schema,),
            )
            value_key = "table_name"
        elif db_type == 'postgresql':
            rows = await self.execute_query(
                db_name,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = $1 "
                "ORDER BY table_name",
                (schema,),
            )
            value_key = "table_name"
        elif db_type == 'sqlite':
            rows = await self.execute_query(
                db_name,
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name",
            )
            value_key = "name"
        else:
            raise NotImplementedError(f"db type {db_type} not supported yet")

        result: list[str] = []
        for row in rows or []:
            value = None
            for k, v in row.items():
                if str(k).lower() == value_key:
                    value = v
                    break
            if value is None:
                continue
            result.append(str(value))
        return result

    async def get_table_schema(
        self, db_name: str, table_name: str, schema: str | None = None,
    ) -> list[dict]:
        """Возвращает список полей и их описаний для указанной таблицы.

        Args:
            db_name: Имя БД из конфигурации.
            table_name: Имя таблицы (без схемы).
            schema: Схема. Если None — берётся conf["database"]
                (для SQLite — "main").

        Returns:
            Список словарей с ключами column_name, data_type, column_comment.
        """
        if not table_name:
            return []

        db_type = self.get_db_type(db_name)
        if schema is None:
            schema = (self.get_db_config(db_name).get("database") or "").strip()
            if db_type == "sqlite":
                schema = "main"

        cache_key = ("columns", db_name, schema or "*", table_name)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        if db_type == 'mysql':
            rows = await self.execute_query(
                db_name,
                "SELECT column_name, data_type, column_comment "
                "FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position",
                (schema, table_name),
            )
            result = []
            for row in rows or []:
                normalized = {str(k).lower(): v for k, v in row.items()}
                col_name = normalized.get("column_name", "")
                if is_technical(col_name):
                    continue
                result.append({
                    "column_name": col_name,
                    "data_type": normalized.get("data_type", ""),
                    "column_comment": normalized.get("column_comment", "") or "",
                })
            self._cache_set(cache_key, result)
            return result

        elif db_type == 'postgresql':
            rows = await self.execute_query(
                db_name,
                "SELECT c.column_name, c.data_type, "
                "       COALESCE(d.description, '') AS column_comment "
                "FROM information_schema.columns c "
                "LEFT JOIN pg_catalog.pg_statio_all_tables st "
                "  ON st.schemaname = c.table_schema AND st.relname = c.table_name "
                "LEFT JOIN pg_catalog.pg_description d "
                "  ON d.objoid = st.relid AND d.objsubid = c.ordinal_position "
                "WHERE c.table_schema = $1 AND c.table_name = $2 "
                "ORDER BY c.ordinal_position",
                (schema, table_name),
            )
            result = [
                dict(row)
                for row in (rows or [])
                if not is_technical(row["column_name"])
            ]
            self._cache_set(cache_key, result)
            return result

        elif db_type == 'sqlite':
            if db_name not in self.pools:
                await self.connect(db_name)
            pool = self.pools[db_name]
            if not re.fullmatch(r'[A-Za-z0-9_]+', table_name):
                logger.warning("Invalid table_name for SQLite PRAGMA: %s", table_name)
                return []
            cursor = await pool.execute(f"PRAGMA table_info({table_name})")
            rows = await cursor.fetchall()
            await cursor.close()
            result = [
                {"column_name": row[1], "data_type": row[2], "column_comment": ""}
                for row in (rows or [])
                if not is_technical(row[1])
            ]
            self._cache_set(cache_key, result)
            return result

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
        self._schema_cache.clear()
