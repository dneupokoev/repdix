# app/core/engine.py

## Назначение модуля

Ядро системы **repdix** — мессенджер-независимый диспетчер сообщений. Принимает `ChatMessage`, выполняет маршрутизацию, валидацию, RBAC-проверки и оркестрирует взаимодействие с LLM и базой данных, возвращая `ChatResponse`. Модуль не привязан к конкретному мессенджеру: адаптеры (Telegram, HTTP и др.) передают сюда нормализованные сообщения.

Реализует:
- Обработку slash-команд (`/help`, `/db`, `/model`, `/sql`, `/clear`, `/tokens`, `/history`, `/compact`, `/settings`, `/models`).
- Детерминированные запросы (имя таблицы, последняя дата).
- LLM-агента с tool-calling циклом (до 5 шагов) и поддержкой инструментов `execute_sql`, `get_relations`, `execute_template`.
- RBAC-фильтрацию SQL (ручной `/sql`, LLM-initiated, шаблоны YAML).
- Аудит вопросов/ответов и SQL-запросов (раздельно `source=manual/llm`).
- Сохранение истории в `SessionStore` + опциональный кэш (Redis).

## Константы и определения

- `dv_file_version` — версия файла (`261028.02`).
- `TIME_MARKERS` — маркеры временных вопросов (`когда`, `последние` и т.п.).
- `STRUCTURE_MARKERS` — маркеры запросов структуры (`поля`, `описание`).
- `ANALYSIS_MARKERS` — маркеры для роутинга в `analysis_model`.
- `TOOLS_DEFINITION` — JSON-схема инструментов, передаваемая LLM. **Важно:** `get_table_schema` сюда не добавлять (см. комментарий в коде и README).

## Функции

### `_build_base_prompt(db_type: str) -> str`
Формирует системный промпт с учётом диалекта БД (`mysql` / `postgresql` / `sqlite`). Содержит правила анализа, вывода, ограничения по схемам БД и структуре таблиц.

## Класс `RepdixEngine`

Главный диспетчер.

### `__init__(db_manager=None, llm_client=None, cache_manager=None, default_db=None, user_manager=None)`
Параметры:
- `db_manager` — менеджер БД (`get_db_config`, `get_db_type`, `get_table_list`, `get_table_schema`, `execute_query`, `get_db_names`).
- `llm_client` — клиент LLM (`chat`, `get_context_length`, `get_model_names`, `resolve_model_name`).
- `cache_manager` — опциональный кэш (Redis-подобный: `get`, `set`, `delete`).
- `default_db` — БД по умолчанию.
- `user_manager` — источник ролей (`get_user_roles`).

Инициализирует `RBACGuard`, `SessionStore`, лимиты (`max_history=10`, `max_rows_in_response`, `max_message_length`), флаг `show_model_in_response`.

### Основные публичные / полупубличные методы

- `process_message(msg: ChatMessage) -> ChatResponse` — **основная точка входа**. Обрабатывает команды, детерминированные запросы и LLM-цикл под `state.lock` (защита от параллельных запросов одного пользователя).
- `_route_query(text, state) -> str` — выбор модели: `analysis_model` только по явным маркерам, иначе `tool_model` (или зафиксированная `state.current_model`).
- `_get_db_schema(state) -> str` — формирует блок «Доступные таблицы и поля» с учётом RBAC. Для админа — все таблицы + поля; для остальных — только разрешённые таблицы/колонки.
- `_describe_table(state, tbl_part, schema_part, rules, all_tables) -> list[str]` — описание одной таблицы для промпта.
- `_format_sql_result(rows, db_name, table_name) -> str` — компактный текстовый вывод (без Markdown-таблиц), обрезка длинных полей и общего объёма.
- `_run_yaml_template(tpl_name, limit, period, state) -> str` — выполнение шаблона из `config/templates.yaml`. Только MySQL. SQL прогоняется через RBAC-гард перед выполнением.
- `_get_relations_text(allowed_tables) -> str` — текстовое описание связей из `config/relations.yaml` с фильтрацией по разрешённым таблицам.
- `_try_get_last_date(state, table_name) -> str | None` — `MAX(<дата>)` для детерминированных временных запросов.
- `_extract_last_table_from_history(state) -> str | None` — последняя таблица из `state.last_sqls`, затем из истории (исключая `information_schema`, `sqlite_master`).
- `_get_default_schema(state) -> str` — схема по умолчанию (`""` для SQLite, `database` или `public`).
- `_get_db_type(state) -> str` — `mysql` / `postgresql` / `sqlite` (с fallback на `mysql`).
- `_save_history(state, user_id, user_text, assistant_text)` — сохранение пары user/assistant, обрезка истории с удалением ведущих `system`-сообщений (не диалоговых).
- `_log_audit(...)`, `_log_sql_audit(sql, duration, status, error, source)` — аудит ответов и SQL (`source` = `manual` | `llm`).

### Приватные хелперы

- `_extract_table_from_sql(sql) -> str | None`
- `_shorten_text(text, max_len=120) -> str`

## Переменные окружения

- `SHOW_MODEL_IN_RESPONSE` (`true`/`false`) — приписывать ли модель к ответу.
- `MAX_ROWS_IN_RESPONSE` (по умолчанию `100`) — лимит строк в ответе.
- `MAX_MESSAGE_LENGTH` (по умолчанию `4000`) — лимит длины ответа.
- `SQL_QUERY_TIMEOUT` (по умолчанию `15`) — для сообщений о таймауте.

## Зависимости (из импортов)

Стандартная библиотека: `asyncio`, `json`, `logging`, `os`, `re`, `time`, `datetime`.

Сторонние:
- `yaml` (PyYAML) — чтение `config/templates.yaml`, `config/relations.yaml`.

Внутрипроектные:
- `app.core.session`: `SessionState`, `SessionStore` — состояние сессии и хранилище.
- `app.core.validator`: `validate_sql`, `log_violation` — синтаксическая проверка SQL и логирование нарушений.
- `app.core.rbac_guard`: `RBACGuard` — RBAC-проверки (методы `validate_sql`, `_load_config`).
- `app.interfaces.base`: `ChatMessage`, `ChatResponse` — DTO на входе/выходе.

Файлы конфигурации:
- `config/templates.yaml` — шаблоны для `execute_template`.
- `config/relations.yaml` — связи таблиц для `get_relations` и подсказок в промпте.
- `config/help.md` — текст `/help`.

## Использование другими модулями

Из импортов и структуры видно:

1. **Адаптеры мессенджеров** (Telegram и др.) — создают `ChatMessage` (с полями `user_id`, `chat_id`, `text`, `status_callback`) и вызывают `RepdixEngine.process_message()`, получая `ChatResponse(text=...)`. `status_callback` используется для промежуточных статусов («🤖 Выполняю ...»).
2. **Точка сборки приложения** (обычно `app/main.py` или фабрика) — конструирует `RepdixEngine(db_manager=..., llm_client=..., cache_manager=..., user_manager=..., default_db=...)`.
3. **`app.core.session`** — предоставляет `SessionStore` (сессии по `user_id`), `SessionState` с полями `history`, `last_sqls`, `current_db`, `current_model`, `last_model_used`, `lock`.
4. **`app.core.validator` / `app.core.rbac_guard`** — вызываются напрямую для валидации SQL (`validation` + `rbac._load_config`, `rbac.validate_sql`). Отметим: `_get_db_schema` обращается к `rbac._config` (полуприватный интерфейс) — это внутренняя зависимость.
5. **`db_manager`** — ожидаемые методы: `get_db_config`, `get_db_type`, `get_db_names`, `get_table_list`, `get_table_schema`, `execute_query`.
6. **`llm_client`** — ожидаемые методы: `chat(messages, tools=..., model_name=..., tool_choice=...)`, `get_context_length(model)`, `get_model_names()`, `resolve_model_name(name)`.
7. **`cache_manager`** — используется с ключом `repdix:history:{user_id}` (TTL 86400) для сохранения/загрузки истории.
8. **`user_manager`** — `get_user_roles(user_id)`; роль `admin` обходит RBAC-фильтры.

## Заметки и ограничения

- **`get_table_schema` не входит в `TOOLS_DEFINITION`** намеренно: модель видит схему в системном промпте, а `SHOW COLUMNS` не прошёл бы `validate_sql` (разрешены только `SELECT` / `WITH`).
- **RBAC для `/sql`**: при успехе `validate_sql` возвращает нормализованный SQL, который подменяет исходный (`sql = rbac_msg`).
- **RBAC для `execute_template`**: собранный SQL обязательно прогоняется через гард — это закрывает дыру, когда шаблон мог обратиться к недоступной таблице.
- **Обрезка истории**: `_save_history` оставляет последние `max_history` сообщений и выкидывает ведущие `system`-сообщения. Реальные SQL-запросы хранятся отдельно в `state.last_sqls` (до 20) и не теряются при обрезке истории.
- **Защита контекста**: при оценке `> 80%` лимита история обрезается до 2 сообщений; если не помогает — предлагается `/compact` или `/clear`.
- **`analysis_model`** вызывается **без tools** — поэтому длинные вопросы роутер в него больше не отправляет.
- **Обрезка ответа** — по `self.max_message_length`, а не по жёсткому `2000`.
- **Автоматический `LIMIT`**: для LLM-SQL добавляется `LIMIT` (10 для транскриптов/dialog_from_whisper, иначе `max_rows_in_response`), если его нет в запросе.
- **Кэш истории** загружается в начале `process_message` при наличии `cache_manager`.
- **Логовые каналы**: `logger` (общий) и `audit_logger` (структурированный JSON-аудит; типы `sql_manual`, `sql_llm`).