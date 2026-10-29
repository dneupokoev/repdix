# app/core/engine.py

## Назначение модуля

Ядро системы **repdix** — мессенджер-независимый диспетчер сообщений. Принимает `ChatMessage`, выполняет маршрутизацию, валидацию, RBAC-проверки и оркестрирует взаимодействие с LLM и базой данных, возвращая `ChatResponse`. Модуль не привязан к конкретному мессенджеру: адаптеры (Telegram, HTTP и др.) передают сюда нормализованные сообщения.

Реализует:
- Обработку slash-команд: `/help`, `/db`, `/db <name>`, `/model`, `/model <name>`, `/models`, `/sql`, `/clear`, `/tokens`, `/history`, `/compact`, `/settings`.
- Детерминированные запросы: имя таблицы и последняя дата по таблице.
- LLM-агента с tool-calling циклом до 5 шагов и инструментами `execute_sql`, `get_relations`, `execute_template`.
- RBAC-фильтрацию SQL: ручной `/sql`, LLM-инициированный `execute_sql`, SQL из YAML-шаблонов.
- Аудит вопросов/ответов и SQL-запросов с разделением `source=manual/llm`.
- Сохранение истории в `SessionStore` и опциональный кэш (Redis-подобный).
- Защиту от параллельных запросов одного пользователя через `state.lock`.

## Константы и определения

- `dv_file_version` — версия файла: `261029.05`.
- `TIME_MARKERS` — маркеры временных вопросов (`когда`, `последние`, `latest` и т.п.).
- `STRUCTURE_MARKERS` — маркеры запросов структуры (`поля`, `описание`, `структура`).
- `ANALYSIS_MARKERS` — маркеры для роутинга в `analysis_model` (`почему`, `сравни`, `тренд` и т.п.).
- `TOOLS_DEFINITION` — JSON-схема инструментов для LLM: `execute_sql`, `get_relations`, `execute_template`. **Важно:** `get_table_schema` сюда не добавлять: модель видит схему в системном промпте, а `SHOW COLUMNS` не прошёл бы `validate_sql`.

## Функции

### `_build_base_prompt(db_type: str) -> str`

Формирует системный промпт с учётом диалекта БД (`mysql`, `postgresql`, `sqlite`). Содержит:
- ограничения диалекта SQL;
- запрет/ограничение запросов к системным каталогам;
- правила анализа, вывода, отказов и обязательного комментария перед вызовом инструмента;
- требование указывать схему перед именем таблицы.

## Класс `RepdixEngine`

Главный диспетчер.

### `__init__(db_manager=None, llm_client=None, cache_manager=None, default_db=None, user_manager=None)`

Параметры:
- `db_manager` — менеджер БД (`get_db_config`, `get_db_type`, `get_table_list`, `get_table_schema`, `execute_query`, `get_db_names`).
- `llm_client` — клиент LLM (`chat`, `get_context_length`, `get_model_names`, `resolve_model_name`).
- `cache_manager` — опциональный кэш (`get`, `set`, `delete`).
- `default_db` — БД по умолчанию.
- `user_manager` — источник ролей (`get_user_roles`).

Инициализирует `RBACGuard`, `SessionStore`, лимиты (`max_history=10`, `max_rows_in_response`, `max_message_length`), флаг `show_model_in_response`. `RBACGuard` создаётся с `technical_columns_getter=None`; дополнительная защита от технических колонок выполняется в `_format_sql_result`.

### Основные публичные / полупубличные методы

- `process_message(msg: ChatMessage) -> ChatResponse` — **основная точка входа**. Проверяет `state.lock`, загружает историю из кэша при наличии `cache_manager`, обрабатывает команды, детерминированные запросы и LLM-цикл. В конце пишет аудит и сохраняет историю.
  - Команды: `/help`, `/clear`, `/tokens`, `/history`, `/compact`, `/settings`, `/db`, `/db <name>`, `/model`, `/model <name>`, `/models`, `/sql <query>`.
  - Детерминированный запрос имени таблицы: если текст состоит из `[a-zA-Z0-9_]+`, проверяется контекст последних сообщений. При временном контексте без структурного вызывается `_try_get_last_date`, иначе возвращается схема таблицы через `get_table_schema`.
  - LLM-путь: выбор модели, сборка системного промпта со схемой БД и текущей датой, проверка переполнения контекста, вызов `analysis_model` без tools или tool-calling цикл до 5 шагов.
  - В tool-loop обрабатываются только `execute_sql`, `get_relations`, `execute_template`. Неизвестные инструменты возвращают ошибку в LLM.
  - Для `execute_sql` выполняются `validate_sql`, RBAC-проверка, автоматическое добавление `LIMIT` при отсутствии, выполнение запроса, форматирование результата, SQL-аудит и сохранение SQL в `state.last_sqls` (до 20).
  - После tool-loop при отсутствии финального текста запрашивается финальный ответ без tools; пустой ответ заменяется предупреждением; длинный ответ обрезается по `max_message_length`; удаляются псевдо-вызовы `execute_sql(...)`.
- `_route_query(text, state) -> str` — выбор модели: `analysis_model` только по явным `ANALYSIS_MARKERS`, иначе `tool_model` (или зафиксированная `state.current_model`). Длинный вопрос сам по себе не отправляется в `analysis_model`.
- `_get_db_schema(state) -> str` — формирует блок «Доступные таблицы и поля» с учётом RBAC. Для `admin` — все таблицы и поля; для остальных — только разрешённые таблицы/колонки, собранные из конфигурации RBAC. Поддерживает wildcard-правила. Добавляет отфильтрованные связи таблиц.
- `_describe_table(state, tbl_part, schema_part, rules, all_tables) -> list[str]` — описание одной таблицы для промпта с учётом `allowed_columns` и `excluded_columns`.
- `_format_sql_result(rows, db_name=None, table_name=None) -> str` — компактный текстовый вывод без Markdown-таблиц. Фильтрует технические колонки; если после фильтрации не осталось колонок, возвращает отказ доступа. Для <5 колонок и >=5 колонок использует нумерованный список; ограничивает строки, длину текстовых полей и общий размер ответа.
- `_run_yaml_template(tpl_name, limit, period, state) -> str` — выполнение шаблона из `config/templates.yaml`. Доступно только для MySQL. Проверяет технические таблицы, валидирует идентификаторы, собирает SQL с учётом периода/лимита и обязательно прогоняет SQL через RBAC-гард перед выполнением.
- `_get_relations_text(allowed_tables=None) -> str` — текстовое описание связей из `config/relations.yaml` с фильтрацией технических таблиц/колонок и, при необходимости, по списку разрешённых таблиц.
- `_try_get_last_date(state, table_name) -> str | None` — ищет `MAX(<дата>)` по типовым датам (`created_at`, `updated_at`, `date`, `timestamp`, `inserted_at`).
- `_extract_last_table_from_history(state) -> str | None` — ищет последнюю таблицу сначала в `state.last_sqls` (в обратном порядке), затем в истории; исключает `information_schema` и `sqlite_master`.
- `_get_default_schema(state) -> str` — схема по умолчанию: `""` для SQLite, имя database для MySQL/PostgreSQL при наличии, иначе `public`.
- `_get_db_type(state) -> str` — `mysql` / `postgresql` / `sqlite` с fallback на `mysql`.
- `_save_history(state, user_id, user_text, assistant_text)` — добавляет пару `user/assistant`, обрезает историю до `max_history`, удаляет ведущие `system`-сообщения при обрезке, сохраняет историю в кэш с TTL `86400`. Текущий код не добавляет `[executed_sql]` в историю; LLM-инициированные SQL хранятся отдельно в `state.last_sqls`.
- `_log_audit(...)` — структурированный аудит вопроса/ответа.
- `_log_sql_audit(sql, duration, status, error=None, source="llm")` — SQL-аудит; `source` = `manual` или `llm`.

### Приватные хелперы

- `_extract_table_from_sql(sql) -> str | None` — извлекает имя таблицы после `FROM` без схемы.
- `_shorten_text(text, max_len=120) -> str` — нормализует пробелы, убирает типовые вступления, обрезает текст.

## Переменные окружения

- `SHOW_MODEL_IN_RESPONSE` (`true`/`false`) — приписывать ли модель к ответу.
- `MAX_ROWS_IN_RESPONSE` (по умолчанию `100`) — лимит строк в ответе и автоматический `LIMIT` для LLM-SQL.
- `MAX_MESSAGE_LENGTH` (по умолчанию `4000`) — лимит длины ответа.
- `SQL_QUERY_TIMEOUT` (по умолчанию `15`) — используется в сообщениях о таймауте SQL.

## Зависимости (из импортов)

Стандартная библиотека: `asyncio`, `json`, `logging`, `os`, `re`, `time`, `datetime`. `asyncio` импортируется, но в текущей реализации явно не используется.

Сторонние:
- `yaml` (PyYAML) — чтение `config/templates.yaml`, `config/relations.yaml`.

Внутрипроектные:
- `app.core.session`: `SessionState`, `SessionStore` — состояние сессии и хранилище.
- `app.core.validator`: `validate_sql`, `log_violation` — проверка SQL и логирование нарушений.
- `app.core.rbac_guard`: `RBACGuard` — RBAC-проверки (`validate_sql`, `_load_config`).
- `app.interfaces.base`: `ChatMessage`, `ChatResponse` — DTO на входе/выходе.

Файлы конфигурации:
- `config/templates.yaml` — шаблоны для `execute_template`.
- `config/relations.yaml` — связи таблиц для `get_relations` и подсказок в промпте.
- `config/help.md` — текст `/help`.

## Использование другими модулями

Из импортов и структуры видно:

1. **Адаптеры мессенджеров** (Telegram и др.) создают `ChatMessage` с полями `user_id`, `chat_id`, `text`, `status_callback` и вызывают `RepdixEngine.process_message()`, получая `ChatResponse(text=...)`. `status_callback` используется для промежуточных статусов («🤖 Выполняю ...»).
2. **Точка сборки приложения** (обычно `app/main.py` или фабрика) конструирует `RepdixEngine(db_manager=..., llm_client=..., cache_manager=..., user_manager=..., default_db=...)`.
3. **`app.core.session`** предоставляет `SessionStore` и `SessionState` с полями `history`, `last_sqls`, `current_db`, `current_model`, `last_model_used`, `lock`.
4. **`app.core.validator` / `app.core.rbac_guard`** вызываются напрямую для валидации SQL. `_get_db_schema` обращается к `rbac._config` (полуприватный интерфейс) — это внутренняя зависимость.
5. **`db_manager`** — ожидаемые методы: `get_db_config`, `get_db_type`, `get_db_names`, `get_table_list`, `get_table_schema`, `execute_query`.
6. **`llm_client`** — ожидаемые методы: `chat(messages, tools=..., model_name=..., tool_choice=...)`, `get_context_length(model)`, `get_model_names()`, `resolve_model_name(name)`.
7. **`cache_manager`** — используется с ключом `repdix:history:{user_id}` (TTL 86400) для сохранения/загрузки истории.
8. **`user_manager`** — `get_user_roles(user_id)`. Роль `admin` обходит обычные RBAC-запреты, но не технический фильтр: технические таблицы/колонки запрещены всем ролям.

## Заметки и ограничения

- **`get_table_schema` не входит в `TOOLS_DEFINITION`** намеренно: модель видит схему в системном промпте, а `SHOW COLUMNS` не прошёл бы `validate_sql` (разрешены только `SELECT` / `WITH`).
- **RBAC для `/sql`**: при успехе `validate_sql` возвращает нормализованный SQL, который подменяет исходный (`sql = rbac_msg`).
- **RBAC для `execute_template`**: собранный SQL обязательно прогоняется через гард — это закрывает дыру, когда шаблон мог обратиться к недоступной таблице.
- **RBAC для LLM `execute_sql` и `/sql`**: проверка выполняется и для администраторов. Админ обходит только обычные RBAC-правила, но не технический фильтр.
- **Обрезка истории**: `_save_history` оставляет последние `max_history` сообщений и выкидывает ведущие `system`-сообщения при обрезке. LLM-инициированные SQL-запросы хранятся отдельно в `state.last_sqls` (до 20) и не теряются при обрезке истории. Ручной `/sql` в `state.last_sqls` не добавляется.
- **Защита контекста**: при оценке `> 80%` лимита история обрезается до 2 сообщений; если не помогает — предлагается `/compact` или `/clear`.
- **`analysis_model`** вызывается без tools, поэтому длинные вопросы сами по себе в него не роутятся.
- **Обрезка ответа** выполняется по `self.max_message_length`, а не по жёсткому `2000`.
- **Автоматический `LIMIT`**: для LLM-SQL добавляется `LIMIT` (10 для `text_transcrib`/`dialog_from_whisper`, иначе `max_rows_in_response`), если его нет в запросе.
- **Кэш истории** загружается в начале `process_message` при наличии `cache_manager`.
- **Логовые каналы**: `logger` (общий) и `audit_logger` (структурированный JSON-аудит; типы `sql_manual`, `sql_llm`).
- **Технические колонки**: `_format_sql_result` дополнительно фильтрует их из результата как защита в глубину. Если после фильтрации не осталось колонок, возвращается отказ доступа, а не «пусто».
- **`get_relations` tool-loop**: обработчик читает `config/relations.yaml` и возвращает связи для указанной таблицы; в этом обработчике нет отдельной RBAC/technical-фильтрации, в отличие от `_get_relations_text` для промпта.