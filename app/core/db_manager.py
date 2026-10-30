# app/core/db_manager.py
dv_file_version = '261030.12'
"""Менеджер подключений к базам данных (MySQL / MariaDB)."""

import asyncio
import logging
import os
import re
import time
from typing import Any

import aiomysql
import yaml
from pymysql.constants import CLIENT

from app.core.technical_filter import is_technical
from app.core.validator import _strip_string_literals

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
        # Сколько строк максимум читать из БД за один запрос (fetchmany).
        # Защита от OOM: fetchall() тянул ВЕСЬ результат в память, и
        # "SELECT * FROM huge" мог уронить процесс.
        try:
            self.fetch_limit = max(
                1, int(os.environ.get("SQL_FETCH_LIMIT") or "1000")
            )
        except (TypeError, ValueError):
            logger.warning(
                "Некорректное значение SQL_FETCH_LIMIT=%r, используем 1000",
                os.environ.get("SQL_FETCH_LIMIT"),
            )
            self.fetch_limit = 1000
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
        """Возвращает тип БД (mysql/mariadb) в нижнем регистре."""
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
          - не задано — fallback [conf["database"]].

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
            if db_type == "mysql" and fallback_db and not is_technical(fallback_db):
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
                    # Не подменяем список одной схемой: LLM получит ложную
                    # картину и сгенерирует SQL по несуществующей таблице.
                    # Пустой список — явный сигнал «схемы недоступны»;
                    # engine._get_db_schema вернёт "" и LLM увидит "—".
                    logger.error(
                        "get_schemas(auto) failed for %s: %s — возвращаю пустой список",
                        db_name, e,
                    )
                    return []
            # Прочие типы БД не поддерживаются — fallback.
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
                for stmt in init_stmts:
                    stripped = _strip_string_literals(stmt)
                    if ";" in stripped:
                        logger.warning(
                            "DB %s: init_statements содержит ';' вне литералов "
                            "(stmt=%r). CLIENT.MULTI_STATEMENTS разорвёт команду.",
                            name, stmt,
                        )
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

            # MULTI_STATEMENTS включаем только если init_cmd содержит ';'
            # (несколько SET). Иначе не расширяем поверхность атаки.
            has_multi = bool(init_cmd) and ";" in init_cmd

            pool = await aiomysql.create_pool(
                host=db_conf.get('host', 'localhost'),
                port=int(db_conf.get('port', 3306)),
                user=db_conf.get('user', ''),
                password=db_conf.get('password', ''),
                db=db_conf.get('database', ''),
                charset=db_conf.get('charset', 'utf8mb4'),
                autocommit=True,
                # SSDictCursor — server-side курсор: fetchmany() читает порциями
                # с сервера, а не из уже загруженного в память буфера. Без этого
                # "SELECT * FROM huge" тянул весь результат в память (OOM).
                cursorclass=aiomysql.SSDictCursor,
                init_command=init_cmd,
                client_flag=CLIENT.MULTI_STATEMENTS if has_multi else 0,
                minsize=int(db_conf.get('minsize', 1)),
                maxsize=int(db_conf.get('maxsize', 5)),
            )
            self.pools[name] = pool
            # Диагностика прав MySQL-пользователя (README 4.1). Проверка
            # не должна ронять connect(): SHOW GRANTS может быть недоступен.
            try:
                await self._check_mysql_grants(name, pool)
            except Exception as e:
                logger.warning(
                    "Ошибка проверки SHOW GRANTS для '%s': %s "
                    "(не блокирует запуск)",
                    name, e,
                )
            return

        else:
            raise NotImplementedError(
                f"db type {db_type} not supported (only mysql/mariadb)"
            )

    async def _check_mysql_grants(self, name: str, pool) -> None:
        """Проверяет права MySQL-пользователя через SHOW GRANTS.

        Выполняет ``SHOW GRANTS FOR CURRENT_USER()`` и пишет WARNING в
        логгер ``security``, если у пользователя БД есть права на запись.
        Это диагностика, а не защита (README 4.1): запуск не блокируется.

        Если SHOW GRANTS недоступен (нет прав / ошибка сервера) — пишет
        WARNING в app-логгер, чтобы админ проверил права вручную.
        Исключения наружу не пробрасываются.

        Args:
            name: Имя БД из конфигурации (для сообщений).
            pool: Пул aiomysql, из которого берётся соединение.
        """
        security_logger = logging.getLogger("security")

        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SHOW GRANTS FOR CURRENT_USER()")
                    rows = await cur.fetchall()
        except Exception as e:
            logger.warning(
                "Не удалось проверить SHOW GRANTS для '%s': %s. "
                "Проверьте права БД вручную (README 4.1).",
                name, e,
            )
            return

        # Админские привилегии (FILE/SUPER/PROCESS/RELOAD/SHUTDOWN/EXECUTE)
        # не дают записи в таблицы, но дают управление сервером — тоже
        # должны попадать в security.log.
        write_keywords = (
            "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER",
            "FILE", "SUPER", "PROCESS", "RELOAD", "SHUTDOWN", "EXECUTE",
        )

        for row in rows or []:
            if not isinstance(row, dict):
                continue
            grant = ""
            for value in row.values():
                grant = str(value or "")
                break
            if not grant:
                continue

            upper = grant.upper()
            has_all = "ALL PRIVILEGES" in upper
            has_write = "ON " in upper and any(
                re.search(r"\b" + kw + r"\b", upper) for kw in write_keywords
            )
            if not (has_all or has_write):
                continue

            security_logger.warning(
                "SECURITY: DB user for '%s' has write privileges: %s. "
                "README 4.1 requires SELECT-only user. "
                "Readonly mode is NOT a replacement for DB grants.",
                name, grant,
            )

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
                используется %s.

        Returns:
            Список словарей с результатами запроса. Читается не более
            ``SQL_FETCH_LIMIT + 1`` строк (fetchmany/серверный курсор):
            полный результат в память не тянется (защита от OOM).
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
                        # fetchmany вместо fetchall: не тянем весь результат
                        # в память. +1 строка — чтобы отличить "ровно limit"
                        # от "есть ещё строки".
                        rows = await cur.fetchmany(self.fetch_limit + 1)
                        return rows if rows is not None else []

            # ВАЖНО: asyncio.wait_for снимает ожидание в Python, но НЕ отменяет
            # серверный запрос — aiomysql не отправляет KILL QUERY. Основная
            # защита — MAX_EXECUTION_TIME (ставится в init_command при создании
            # пула, включая readonly=true). wait_for — страховка от зависания
            # Python, если серверный таймаут не сработал.
            try:
                return await asyncio.wait_for(
                    _run_mysql_query(), timeout=self.query_timeout + 2,
                )
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"Query timeout exceeded ({self.query_timeout} sec); "
                    "серверный запрос мог продолжить выполнение — "
                    "проверьте slow_query_log."
                )

        else:
            raise NotImplementedError(
                f"db type {db_type} not supported (only mysql/mariadb)"
            )

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
        else:
            raise NotImplementedError(
                f"db type {db_type} not supported (only mysql/mariadb)"
            )

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
            schema: Схема. Если None — берётся conf["database"].

        Returns:
            Список словарей с ключами column_name, data_type, column_comment.
        """
        if not table_name:
            return []

        db_type = self.get_db_type(db_name)
        if schema is None:
            schema = (self.get_db_config(db_name).get("database") or "").strip()

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

        else:
            raise NotImplementedError(
                f"db type {db_type} not supported (only mysql/mariadb)"
            )

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
                # aiomysql.Pool: имеет close() и wait_closed().
                # Различаем через hasattr(wait_closed).
                if hasattr(pool, "wait_closed"):
                    pool.close()
                    await pool.wait_closed()
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
