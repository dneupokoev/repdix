# Changelog

## 2026-10-30

### Добавлено
- session: фоновая очистка просроченных сессий (start_cleanup_task/stop_cleanup_task) + интеграция в main.py — app/core/session.py, app/main.py
- tests: tests/test_session_cleanup.py — покрытие фоновой очистки TTL

### Исправлено
- _extract_table_from_sql переведён на sqlglot (кавычки, CTE, подзапросы) — app/core/engine.py
- db_manager: get_schemas(auto) при сбое возвращает [] вместо fallback-схемы (LLM не получает ложную картину) — app/core/db_manager.py
- rbac_guard: неквалифицированные колонки при нескольких таблицах проверяются по всем таблицам (было any, стало all) — app/core/rbac_guard.py
- validator: DANGEROUS_PATTERNS ищутся только вне строковых литералов (ложные отказы на 'SET GLOBAL x') — app/core/validator.py
- engine: неудача expand_select_star пишется в audit.log, а не в security.log — app/core/engine.py

## [2026-10-29]

### Добавлено
- Multi-schema: поддержка нескольких схем в одной MySQL-БД. databases.yaml → schemas: auto / список, exclude_schemas. db_manager.get_schemas(), get_table_list() возвращает fully-qualified имена, get_table_schema(db, table, schema).
- Кэш схемы БД (SCHEMA_CACHE_TTL_SEC, по умолчанию 3600).
- description в databases.yaml — отображается в /db, /db <name>, /settings, ошибке «БД не найдена».
- rbac_guard.get_allowed_tables() и get_allowed_schemas() — RBAC по нескольким схемам.
- /settings — «Доступные схемы», «Схема по умолчанию», «Доступные БД» с описаниями.
- tests/test_db_manager_schemas.py (9), tests/test_rbac_guard_multi_schema.py (6).

### Изменено
- db_manager.get_table_list() — breaking change: fully-qualified имена.
- engine._extract_table_from_sql() — возвращает (schema, table).
- engine._format_sql_result() — принимает schema.
- engine._build_base_prompt() — принимает schemas и default_schema.
- technical_filter.is_technical_qualified() — проверяет все части имени.

### Исправлено
- test_format.py — переведён на async API.
- test_delete_in_comment_is_known_false_positive → test_delete_in_comment_is_ignored.

## [Unreleased]

### Added
- feat: добавить проверку сложности SQL перед выполнением
- feat: добавь lock timeout для MySQL и MariaDB в db_manager
- feat: добавить авто-сжатие истории и динамическую схему БД
- feat: добавь HTML-разметку ответов и parse_mode в Telegram
- feat: добавить поддержку правил вида "schema.*" в RBACGuard
- feat: добавить поддержку нескольких схем и кэш метаданных БД
- feat: добавить фильтрацию технических таблиц и колонок
- feat: добавить модуль RBACGuard для контроля доступа к таблицам и полям SQL
- feat: добавить настройку команд бота в telegram_adapter.py
- feat: добавить FSM-регистрацию пользователей и одобрение заявок администраторами
- feat: добавить обработку команды /help в engine.py
- feat: чистый релиз repdix (без секретов)

### Fixed
- fix: исправить обработку SQL-комментариев без пробела после --
- fix: отправлять статусы с HTML-разметкой без обрезки тегов
- fix: усилить правило вызова get_table_schema перед execute_sql

### Changed
- refactor: удалить deep_analysis_model, оставить tool_calling_model

### Documentation
- docs: обновить комментарии и регулярное выражение для валидации SQL
