# app/core/engine.py

## Назначение модуля

Ядро системы **repdix** — мессенджер-независимый диспетчер сообщений. Принимает `ChatMessage`, выполняет маршрутизацию, валидацию, RBAC-проверки и оркестрирует взаимодействие с LLM и базой данных, возвращая `ChatResponse`. Модуль не привязан к конкретному мессенджеру: адаптеры Telegram, HTTP и др. передают сюда нормализованные сообщения.

Версия файла: `261029.08`.

Модуль реализует:
- обработку slash-команд: `/help`, `/db`, `/db <name>`, `/model`, `/model <name>`, `/models`, `/sql`, `/clear`, `/tokens`, `/history`, `/compact`, `/settings`;
- детерминированные запросы по имени таблицы: последняя дата или структура таблицы;
- LLM-агента с tool-calling циклом до 5 шагов и инструментами `execute_sql`, `get_relations`, `execute_template`;
- multi-schema RBAC-фильтрацию: доступные схемы, таблицы и поля;
- RBAC-проверку SQL для ручного `/sql`, LLM-инициированного `execute_sql` и SQL из YAML-шаблонов;
- аудит вопросов/ответов и SQL-запросов с разделением `source=manual/llm`;
- сохранение истории в `SessionStore` и опциональный кэш;
- защиту от параллельных запросов одного пользователя через `state.lock`.

## Константы и TOOLS_DEFINITION

- `dv_file_version` — версия файла: `261029.08`.
- `TIME_MARKERS` — маркеры временных вопросов: `когда`, `дата`, `время`, `последние`, `последняя`, `свежие`, `newest`, `latest`.
- `STRUCTURE_MARKERS` — маркеры запросов структуры: `описание`, `поля`, `структура`, `метаданные`.
- `ANALYSIS_MARKERS` — маркеры для роутинга в `analysis_model`: `почему`, `проанализируй`, `сравни`, `тренд`, `динамика`, `причина`, `объясни`, `прогноз`, `статистика`, `аналитика`.
- `TOOLS_DEFINITION` — JSON-схема инструментов для LLM: `execute_sql`, `get_relations`, `execute_template`. **Важно:** `get_table_schema` сюда не добавлять: модель видит схему в системном промпте, а `SHOW COLUMNS` не прошёл бы `validate_sql` (разрешены только `SELECT`/`WITH`).

## Функция `_build_base_prompt`

### `_build_base_prompt(db_type: str, schemas: list[str] | None = None, default_schema: str = "") -> str`

Формирует системный промпт с учётом диалекта БД: `mysql`, `postgresql` или `sqlite`. Содержит:
- ограничения диалекта SQL;
- запрет/ограничение запросов к системным каталогам;
- список переданных схем и схему по умолчанию;
- требование всегда указывать схему перед именем таблицы;
- правила analysis-поведения: несколько SQL для сложных вопросов, краткий вывод, запрет Markdown-таблиц с `|`, обработка отказов;
- требование писать краткий комментарий перед каждым вызовом инструмента;
- запрет использовать таблицы/поля вне блока «Доступные таблицы и поля».

В текущем вызове `process_message` передаёт в `schemas` список схем из `db_manager.get_schemas(current_db)` (без RBAC-фильтрации), а RBAC-отфильтрованный блок схем и таблиц отдельно добавляется через `_get_db_schema(state)`.

## Класс `RepdixEngine`

Главный диспетчер.

### `__init__(db_manager=None, llm_client=None, cache_manager=None, default_db: str | None = None, user_manager=None)`

Параметры:
- `db_manager` — менеджер БД;
- `llm_client` — клиент LLM;
- `cache_manager` — опциональный кэш;
- `default_db` — БД по умолчанию;
- `user_manager` — источник ролей пользователя.

Инициализирует `RBACGuard`, `SessionStore`, лимиты и настройки:
- `max_history=10`;
- `tool_model="tool_calling_model"`;
- `analysis_model="deep_analysis_model"`;
- `show_model_in_response` из `SHOW_MODEL_IN_RESPONSE`;
- `max_rows_in_response` из `MAX_ROWS_IN_RESPONSE`, по умолчанию `100`;
- `max_message_length` из `MAX_MESSAGE_LENGTH`, по умолчанию `4000`.

`RBACGuard` создаётся с `technical_columns_getter=None`: `RBACGuard.validate_sql` синхронный, а `db_manager` асинхронный, поэтому дополнительная защита от технических колонок выполняется фильтрацией результата в `_format_sql_result()`.

### `process_message(msg: ChatMessage) -> ChatResponse`

Основная точка входа. Проверяет `state.lock`: если запрос уже обрабатывается, возвращает сообщение ожидания. Иначе захватывает lock и гарантированно освобождает его в `finally`.

Порядок обработки:
1. Вычисляет `default_schema` для текущей БД.
2. Обрабатывает точные команды `/help`, `/clear`, `/tokens`, `/history`, `/compact`, `/settings`, `/db`, `/db <name>`, `/model`, `/model <name>`, `/models`, `/sql <query>`.
3. Обрабатывает детерминированный запрос имени таблицы.
4. Если `llm_client` отсутствует — возвращает `Эхо: <text>`.
5. Иначе строит системный промпт, проверяет контекст, выбирает модель и запускает LLM-путь: `analysis_model` без tools или tool-calling цикл.
6. Сохраняет историю, пишет аудит и возвращает `ChatResponse`.

История из кэша загружается после `/help` и до остальных команд при наличии `cache_manager`. Ключ кэша: `repdix:history:{user_id}`.

#### Команды

- `/help` — читает `config/help.md`; при отсутствии файла возвращает подсказку использовать `/db`, `/model`, `/sql`.
- `/clear` — очищает историю и удаляет её из кэша.
- `/tokens` — оценивает количество сообщений, лимит контекста и примерный размер истории в токенах (`total_chars // 4`).
- `/history` — выводит историю с ролями и обрезкой каждого сообщения до 300 символов.
- `/compact` — сжимает историю через LLM в краткое резюме; требует至少 4 сообщений и подключённый `llm_client`. Сохраняет резюме как `system`-сообщение и обновляет кэш.
- `/settings` — показывает активную модель, лимит истории, лимит контекста, доступные БД и их display-имена, текущую БД, доступные схемы с учётом RBAC, схему по умолчанию, доступные модели и `SQL_QUERY_TIMEOUT`.
- `/db <name>` — переключает текущую БД, если имя есть в `get_db_names()`; показывает display-имя и схему по умолчанию.
- `/db` — показывает текущую БД и список доступных БД.
- `/model <name>` — разрешает имя модели через `resolve_model_name()` и фиксирует модель в сессии.
- `/model` — показывает текущую модель или режим авто-роутера.
- `/models` — выводит список доступных моделей.
- `/sql <query>` — ручной SQL:
  - вызывает `validate_sql(sql)`;
  - при наличии `user_manager` выполняет RBAC-проверку;
  - администратор обходит обычные RBAC-запреты, но не технический фильтр;
  - при успешной RBAC-проверке подменяет SQL нормализованным `rbac_msg`;
  - требует выбранную БД;
  - выполняет запрос, форматирует результат через `_format_sql_result`, пишет SQL-аудит с `source="manual"`;
  - не добавляет ручной SQL в `state.last_sqls` и не добавляет автоматический `LIMIT`.

#### Детерминированный запрос имени таблицы

Если выбрана БД и текст полностью соответствует `[a-zA-Z0-9_]+`:
- анализирует последние 2 сообщения истории;
- если есть временной контекст (`TIME_MARKERS`) и нет структурного (`STRUCTURE_MARKERS`), пытается получить последнюю дату через `_try_get_last_date`;
- иначе получает структуру таблицы через `db_manager.get_table_schema` и форматирует её через `_format_sql_result`;
- отдельная RBAC-проверка в этой ветке не выполняется.

#### LLM-путь

- `_route_query()` выбирает модель: зафиксированная `state.current_model`, иначе `analysis_model` только при наличии `ANALYSIS_MARKERS`, иначе `tool_model`.
- Собирает `base_prompt`, текущую дату, RBAC-отфильтрованную схему через `_get_db_schema`.
- При временных маркерах добавляет подсказку о последней таблице из `state.last_sqls` или истории.
- Проверяет переполнение контекста: если оценка `> 80%` лимита, история обрезается до последних 2 сообщений; если это не помогает — предлагается `/compact` или `/clear`.
- `analysis_model` вызывается без tools, затем сохраняет историю и возвращает ответ.
- Для `tool_model` запускается агентский цикл до 5 шагов:
  - перед вызовом инструмента при наличии `status_callback` отправляется краткий статус;
  - неизвестные инструменты возвращают ошибку в LLM;
  - `execute_template` вызывает `_run_yaml_template`;
  - `get_relations` читает `config/relations.yaml` и возвращает связи по имени таблицы; отдельной RBAC/technical-фильтрации в этом обработчике нет;
  - `execute_sql` валидируется через `validate_sql`, проверяется RBAC, при отсутствии `LIMIT` добавляется `LIMIT` (`10` для `text_transcrib`/`dialog_from_whisper`, иначе `max_rows_in_response`), выполняется запрос, результат форматируется, пишется SQL-аудит с `source="llm"`, SQL успешно выполненного запроса сохраняется в `state.last_sqls` (до 20).
- После цикла при отсутствии финального текста запрашивается финальный ответ без tools.
- Пустой ответ заменяется предупреждением.
- Длинные ответы с признаками отказа/извинений при длине > 150 символов заменяются на `⛔ Нет доступа к данным.`
- Итоговый текст обрезается по `max_message_length`, из него удаляются псевдо-вызовы `execute_sql(...)`.
- История сохраняется через `_save_history`.

### Вспомогательные и служебные методы

- `_get_default_schema(state) -> str` — схема по умолчанию: `""` для SQLite; `database` из конфигурации для MySQL/PostgreSQL; fallback: имя текущей БД для MySQL, иначе `public`.
- `_get_db_type(state) -> str` — `mysql`, `postgresql` или `sqlite`; fallback `mysql`.
- `_format_sql_result(rows, db_name=None, schema=None, table_name=None) -> str` — компактный текстовый вывод без Markdown-таблиц. Фильтрует технические колонки через `is_technical`. Если все колонки технические — возвращает отказ доступа. Иначе формирует нумерованный список строк с парами `колонка: значение`, ограничивает число строк через `max_rows_in_response`, длину текстовых полей до 4000, обычных — до 50, общий размер — через `max_message_length`. Для определения текстовых колонок может вызывать `db_manager.get_table_schema(db_name, table_name, schema)`.
- `_extract_table_from_sql(sql) -> tuple[str | None, str] | None` — извлекает `(schema, table)` из первого `FROM`; `schema=None`, если схема не указана.
- `_split_qualified(name, default_schema="") -> tuple[str, str]` — делит `schema.table`; для короткого имени подставляет `default_schema`.
- `_shorten_text(text, max_len=120) -> str` — нормализует пробелы, убирает типовые вступления, обрезает текст.
- `_route_query(text, state) -> str` — выбор модели: `analysis_model` только по явным `ANALYSIS_MARKERS`, иначе `tool_model`; учитывает `state.current_model`.
- `_extract_last_table_from_history(state) -> str | None` — ищет последнюю таблицу сначала в `state.last_sqls` в обратном порядке, затем в истории; исключает `information_schema` и `sqlite_master`; возвращает `schema.table`, если схема была указана.
- `_try_get_last_date(state, table_name) -> str | None` — ищет `MAX(<дата>)` по типовым колонкам `created_at`, `updated_at`, `date`, `timestamp`, `inserted_at`.
- `_run_yaml_template(tpl_name, limit, period, state) -> str` — выполняет шаблон из `config/templates.yaml`. Доступно только для MySQL. Проверяет техническую таблицу, валидирует идентификаторы, собирает SQL с периодом/лимитом/action-фильтром и перед выполнением прогоняет SQL через RBAC-гард при наличии `user_manager`. Админ обходит только обычные RBAC-правила, но не технический фильтр. Нормализованный `rbac_msg` здесь не подставляется в SQL.
- `_get_relations_text(allowed_tables=None) -> str` — читает `config/relations.yaml`, фильтрует технические таблицы/колонки через `is_technical_qualified`, при необходимости фильтрует по списку fully-qualified таблиц.
- `_get_db_schema(state) -> str` — формирует блок «Доступные таблицы и поля» с учётом RBAC:
  - получает схемы через `db_manager.get_schemas`;
  - получает таблицы через `db_manager.get_table_list`;
  - для admin разрешает все схемы, wildcard `*`, поля не фильтрует;
  - для остальных использует `rbac.get_allowed_schemas`, `rbac.get_allowed_tables`, `_collect_rules`, `_match_table_rule`;
  - поддерживает wildcard-правила и fully-qualified имена `schema.table`;
  - добавляет отфильтрованные связи таблиц.
- `_describe_table(state, schema, table, rules, all_tables) -> list[str]` — описание одной таблицы для промпта. Находит реальное имя таблицы в `all_tables`, получает колонки через `db_manager.get_table_schema(state.current_db, real_name, schema or None)`, применяет `allowed`/`excluded`/`has_wildcard` из RBAC-правила.
- `_save_history(state, user_id, user_text, assistant_text) -> None` — добавляет пару `user/assistant`, обрезает историю до `max_history`, удаляет ведущие `system`-сообщения при обрезке, сохраняет историю в кэш с TTL `86400`. Текущий код не добавляет `[executed_sql]` в историю; LLM-инициированные SQL хранятся отдельно в `state.last_sqls`.
- `_log_audit(state, msg, question, answer) -> None` — структурированный JSON-аудит вопроса/ответа: timestamp, user_id, chat_id, question, answer, db, model, model_used.
- `_log_sql_audit(sql, duration, status, error=None, source="llm") -> None` — SQL-аудит; `type` = `sql_manual` или `sql_llm`, содержит SQL, длительность, статус и ошибку при наличии.

## Переменные окружения

- `SHOW_MODEL_IN_RESPONSE` (`true`/`false`) — приписывать ли модель к ответу.
- `MAX_ROWS_IN_RESPONSE` (по умолчанию `100`) — лимит строк в ответе и автоматический `LIMIT` для LLM-SQL.
- `MAX_MESSAGE_LENGTH` (по умолчанию `4000`) — лимит длины ответа и форматированного SQL-результата.
- `SQL_QUERY_TIMEOUT` (по умолчанию `15`) — используется в сообщениях о таймауте SQL.

## Зависимости (из импортов)

Стандартная библиотека:
- `asyncio` — импортируется, но в текущей реализации явно не используется;
- `json`, `logging`, `os`, `re`, `time`;
- `datetime` из `datetime`.

Сторонние:
- `yaml` (PyYAML) — чтение `config/templates.yaml` и `config/relations.yaml`.

Внутрипроектные:
- `app.core.session`: `SessionState`, `SessionStore` — состояние сессии и хранилище.
- `app.core.technical_filter`: `is_technical`, `is_technical_qualified` — проверка технических таблиц/колонок.
- `app.core.validator`: `validate_sql`, `log_violation` — проверка SQL и логирование нарушений.
- `app.core.rbac_guard`: `RBACGuard` — RBAC-проверки.
- `app.interfaces.base`: `ChatMessage`, `ChatResponse` — DTO на входе/выходе.

Файлы конфигурации:
- `config/templates.yaml` — шаблоны для `execute_template`;
- `config/relations.yaml` — связи таблиц для `get_relations` и подсказок в промпте;
- `config/help.md` — текст `/help`.

Ожидаемые интерфейсы:
- `db_manager`: `get_db_config`, `get_db_type`, `get_db_names`, `get_db_display_name`, `get_schemas`, `get_table_list`, `get_table_schema`, `execute_query`.
- `llm_client`: `chat(messages, tools=..., model_name=..., tool_choice=...)`, `get_context_length(model)`, `get_model_names()`, `resolve_model_name(name)`.
- `cache_manager`: `get`, `set`, `delete`.
- `user_manager`: `get_user_roles(user_id)`.
- `RBACGuard`: `validate_sql`, `get_allowed_schemas`, `get_allowed_tables`, `_load_config`, `_collect_rules`, `_match_table_rule`.

## Использование другими модулями проекта

1. **Адаптеры мессенджеров** создают `ChatMessage` с полями `user_id`, `chat_id`, `text`, `status_callback` и вызывают `RepdixEngine.process_message()`, получая `ChatResponse(text=...)`. `status_callback` используется для промежуточных статусов («🤖 Выполняю ...»).
2. **Точка сборки приложения** обычно конструирует `RepdixEngine(db_manager=..., llm_client=..., cache_manager=..., user_manager=..., default_db=...)`.
3. **`app.core.session`** предоставляет `SessionStore` и `SessionState` с полями `history`, `last_sqls`, `current_db`, `current_model`, `last_model_used`, `lock`.
4. **`app.core.validator` / `app.core.rbac_guard`** вызываются напрямую для валидации SQL и RBAC-проверок. `_get_db_schema` обращается к полуприватным методам `rbac._load_config`, `rbac._collect_rules`, `rbac._match_table_rule`.
5. **`db_manager`** используется для получения конфигурации БД, списка БД, схем, таблиц, структуры таблиц и выполнения запросов.
6. **`llm_client`** используется для чата, оценки контекста, списка моделей и разрешения имени модели.
7. **`cache_manager`** используется с ключом `repdix:history:{user_id}` и TTL `86400` для сохранения/загрузки истории.
8. **`user_manager`** предоставляет роли. Роль `admin` обходит обычные RBAC-запреты, но не технический фильтр: технические таблицы/колонки запрещены всем ролям.

## Заметки и ограничения

- **`get_table_schema` не входит в `TOOLS_DEFINITION`** намеренно: модель видит схему в системном промпте, а `SHOW COLUMNS` не прошёл бы `validate_sql`.
- **RBAC для `/sql` и LLM `execute_sql`**: при успехе `validate_sql` возвращает нормализованный SQL, который подменяет исходный (`sql = rbac_msg`). Для `_run_yaml_template` нормализованный SQL не подставляется.
- **RBAC для `execute_template`**: собранный SQL обязательно прогоняется через гард при наличии `user_manager` — это закрывает дыру, когда шаблон мог обратиться к недоступной таблице.
- **RBAC-проверки в `process_message`** для `/sql` и `execute_sql` выполняются только при наличии `user_manager`. `_get_db_schema` при этом всё равно использует `RBACGuard` с пустыми ролями.
- **Админ и технический фильтр**: администратор обходит только обычные RBAC-правила, но не технический фильтр. Признак технического отказа определяется по подстроке `technical` в сообщении RBAC.
- **Обрезка истории**: `_save_history` оставляет последние `max_history` сообщений и удаляет ведущие `system`-сообщения при обрезке. LLM-инициированные SQL-запросы хранятся отдельно в `state.last_sqls` (до 20) и не теряются при обрезке истории. Ручной `/sql` в `state.last_sqls` не добавляется. В `state.last_sqls` попадает SQL после RBAC-нормализации и авто-`LIMIT`, только при успешном выполнении.
- **Защита контекста**: при оценке `> 80%` лимита история обрезается до 2 сообщений; если не помогает — предлагается `/compact` или `/clear`.
- **`analysis_model`** вызывается без tools, поэтому длинные вопросы сами по себе в него не роутятся.
- **Обрезка ответа** выполняется по `self.max_message_length`, а не по жёсткому `2000`.
- **Автоматический `LIMIT`**: для LLM-SQL добавляется `LIMIT` (10 для `text_transcrib`/`dialog_from_whisper`, иначе `max_rows_in_response`), если его нет в запросе. Проверка наличия `LIMIT` выполняется по подстроке `limit` без учёта положения и завершающей `;`.
- **Кэш истории** загружается в начале `process_message` при наличии `cache_manager`; при наличии кэша он перезаписывает `state.history`.
- **Логовые каналы**: `logger` — общий, `audit_logger` — структурированный JSON-аудит; типы SQL-аудита `sql_manual`, `sql_llm`.
- **Технические колонки**: `_format_sql_result` дополнительно фильтрует их из результата как защита в глубину. Если после фильтрации не осталось колонок, возвращается отказ доступа, а не «пусто».
- **`get_relations` tool-loop**: обработчик читает `config/relations.yaml` и возвращает связи для указанной таблицы; в этом обработчике нет отдельной RBAC/technical-фильтрации, в отличие от `_get_relations_text` для промпта.
- **Детерминированная ветка по имени таблицы** не вызывает `validate_sql`/`RBACGuard` и доступна при выбранной БД; она возвращает структуру таблицы или последнюю дату.
- **`_build_base_prompt`** получает список схем из `db_manager.get_schemas(current_db)` без RBAC-фильтрации; RBAC-отфильтрованные схемы и таблицы добавляются отдельно через `_get_db_schema`.
- **`asyncio`** импортируется, но в текущей реализации явно не используется.