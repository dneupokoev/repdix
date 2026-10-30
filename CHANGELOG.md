# Changelog

## 2026-10-30

### Изменено
- **Удалена поддержка PostgreSQL и SQLite.** Проект теперь работает только с MySQL/MariaDB.
  - `app/core/db_manager.py` — удалены ветки `postgresql`/`sqlite` (импорты `asyncpg`/`aiosqlite`, `connect`, `execute_query`, `get_schemas`, `_fetch_tables`, `get_table_schema`, `close`).
  - `app/core/sql_complexity.py` — `_DIALECT_ALIASES` оставлен только `mysql`/`mariadb`; неизвестный диалект → `ValueError`.
  - `app/core/engine.py` — `_get_default_schema`, `_quote_ident`, `_build_base_prompt` упрощены до MySQL/MariaDB.
  - `Pipfile` — удалены зависимости `asyncpg`, `aiosqlite`.
- **`README.md`** — разделы 1, 2, 3, 4, 8.4, 10 актуализированы под MySQL/MariaDB; убраны упоминания PostgreSQL/SQLite.

### Удалено
- `tests/test_db_manager_pg_pool_sizes.py` — проверял PostgreSQL-пул, которого больше нет.

### Исправлено
- `tests/test_engine_active_check.py::test_summary_in_system_prompt` — добавлен мок `get_db_type` после ужесточения `_build_base_prompt`.

## 2026-10-29

### Добавлено
- **Multi-schema:** поддержка нескольких схем в одной MySQL-БД (`databases.yaml` → `schemas: auto` / список, `exclude_schemas`). `get_table_list()` возвращает fully-qualified имена, `get_table_schema(db, table, schema)`.
- Кэш схемы БД (`SCHEMA_CACHE_TTL_SEC`, по умолчанию 3600).
- `description` в `databases.yaml` — отображается в `/db`, `/settings`, ошибке «БД не найдена».
- `rbac_guard.get_allowed_tables()` и `get_allowed_schemas()` — RBAC по нескольким схемам.
- `/settings` — «Доступные схемы», «Схема по умолчанию», «Доступные БД» с описаниями.
- `tests/test_db_manager_schemas.py`, `tests/test_rbac_guard_multi_schema.py`.

### Изменено
- `db_manager.get_table_list()` — breaking change: fully-qualified имена.
- `engine._extract_table_from_sql()` — возвращает `(schema, table)`.
- `engine._format_sql_result()` — принимает `schema`.
- `engine._build_base_prompt()` — принимает `schemas` и `default_schema`.
- `technical_filter.is_technical_qualified()` — проверяет все части имени.

### Исправлено
- `test_format.py` — переведён на async API.
- `test_delete_in_comment_is_known_false_positive` → `test_delete_in_comment_is_ignored`.

## 2026-10-30 (ранее в этот день)

### Добавлено
- `db_manager.py` — проверка `SHOW GRANTS FOR CURRENT_USER()` при старте MySQL-пула: WARNING в `security.log` при правах на запись (`INSERT`/`UPDATE`/`DELETE`/`CREATE`/`DROP`/`ALTER`/`ALL PRIVILEGES`) или админских привилегиях (`FILE`/`SUPER`/`PROCESS`/`RELOAD`/`SHUTDOWN`/`EXECUTE`). Запуск не блокируется — см. README 4.1.
- `session.py` — фоновая очистка просроченных сессий (`start_cleanup_task`/`stop_cleanup_task`), интеграция в `main.py`.
- `tests/test_session_cleanup.py` — покрытие фоновой очистки TTL.

### Изменено
- `engine.py` — кэш `text_columns` в `_format_sql_result` (меньше запросов к `information_schema`).

### Исправлено
- `db_manager.py` — `fetchmany` вместо `fetchall` (`SQL_FETCH_LIMIT=1000`); `engine.py` — auto-LIMIT для `/sql`.
- `_extract_table_from_sql` и `_extract_last_table_from_history` переведены на `sqlglot` (кавычки, CTE, подзапросы) — `app/core/engine.py`.
- `db_manager.get_schemas(auto)` при сбое возвращает `[]` вместо fallback-схемы — LLM не получает ложную картину.
- `rbac_guard`: неквалифицированные колонки при нескольких таблицах проверяются по всем таблицам (было `any`, стало `all`).
- `validator`: `DANGEROUS_PATTERNS` ищутся только вне строковых литералов (ложные отказы на `'SET GLOBAL x'`).
- `engine`: неудача `expand_select_star` пишется в `audit.log`, а не в `security.log`.
- `engine.py`: не подставлять в `sql` результат `validate_sql`, если это не SQL (защита от изменения контракта).
- `sql_complexity.py`: `RecursionError` в `_max_subquery_depth` превращается в `SQLTooComplex` (fail-closed).
- `db_manager.py`: `_check_mysql_grants` ловит `FILE`/`SUPER`/`PROCESS`/`RELOAD`/`SHUTDOWN`/`EXECUTE`.
- `main.py`: `load_dotenv` вызывается до `_setup_logging` (`LOG_MAX_BYTES` из `.env` применяется).