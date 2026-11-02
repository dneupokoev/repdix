# Changelog

## 2026-11-03

### Добавлено
- **Метка старта/остановки и ротация file-логов.** При запуске в лог пишется
  `REPDIX START | version=... | started_at=... | pid=...`, при завершении —
  `REPDIX STOP | version=... | stopped_at=...`, чтобы после обновления/пересоздания
  контейнера было видно, какая версия поднялась. `audit.log` и `security.log`
  пишутся через `RotatingFileHandler` (размеры/бэкапы из
  `LOG_MAX_BYTES`/`LOG_BACKUP_COUNT` и `SECURITY_LOG_MAX_BYTES`/`SECURITY_LOG_BACKUP_COUNT`),
  а `load_dotenv` вызывается до `_setup_logging`, чтобы эти параметры из `.env`
  применялись.
  - `app/main.py` — `_log_startup_banner()`, `_env_int()`, ротация в `_setup_logging()`,
    метка `REPDIX STOP` в `_shutdown()`; `dv_file_version` → `261103.01`.
- **Аудит: сквозной `turn_id`.** Каждый входящий запрос получает идентификатор хода
  (`state.current_turn_id`), который связывает запись `question/answer` и все записи
  `sql_llm`/`sql_manual` в `audit.log`.
  - `app/core/engine.py` — `process_message()` генерирует `current_turn_id` и сбрасывает
    `current_turn_step` / `current_turn_sql_count` / `current_turn_started_at`
    для каждого сообщения (включая команды).
  - `app/core/engine.py` — `_log_audit()` пишет `turn_id`, `turn`, `status`
    (`ok`/`failed`/`truncated`), `sql_count`, `steps_used`, `duration_sec`.
  - `app/core/engine.py` — `_log_sql_audit()` принимает `state`/`user_id`/`chat_id`/`tool`
    и пишет `turn_id`, `step`, `user_id`, `chat_id`, `tool`
    (`execute_sql` / `manual_sql`).
  - `app/core/engine.py` — новый `_log_turn_summary()` (запись `type: turn_summary`)
    пишется один раз в конце LLM-хода; для служебных команд не пишется.
- `README.md` — раздел «Архитектура» (3.1, 3.2) дополнен описанием сквозного аудита по `turn_id`.

### Изменено
- **Нарезка длинных сообщений Telegram (`_split_long_text`).** Приоритет границ
  разреза расширен: перевод строки → граница предложения (`. `, `! `, `? ` во
  второй половине окна) → пробел → жёсткая нарезка. Незакрытые безопасные теги
  (`<b>`, `<i>`, `<code>`, `<pre>`, `<a>`) закрываются в конце части и заново
  открываются в начале следующей (для `<a>` сохраняется `href`) — каждая часть
  остаётся валидным HTML.
  - `app/interfaces/telegram_adapter.py` — обновлена `_split_long_text()`,
    добавлен хелпер `_track_open_tags()`; `dv_file_version` → `261030.03`.
- `README.md` — разделы 3 и 3.1 (Telegram-адаптер) актуализированы под новое
  поведение нарезки.

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
