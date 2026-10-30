# app/core/engine.py

## Назначение модуля

Ядро системы **repdix** — мессенджер-независимый диспетчер сообщений. Принимает `ChatMessage`, выполняет маршрутизацию, slash-команды, валидацию SQL, RBAC-проверки, работу с LLM и БД, возвращает `ChatResponse`. Модуль не привязан к конкретному мессенджеру: адаптеры Telegram, HTTP и др. передают сюда нормализованные сообщения.

Версия файла: `dv_file_version = '261030.01'`. В docstring перечислены ключевые изменения вплоть до `261030.04`.

Модуль реализует:
- обработку slash-команд: `/help`, `/db`, `/db <name>`, `/model`, `/model <name>`, `/models`, `/sql`, `/clear`, `/tokens`, `/history`, `/compact`, `/settings`, `/remember`, `/memory`, `/forget`, `/reset`;
- детерминированные запросы по имени таблицы: последняя дата или структура таблицы;
- LLM-агента с tool-calling циклом до `AGENT_MAX_STEPS` шагов и инструментами `execute_sql`, `get_relations`;
- multi-schema RBAC-фильтрацию доступных схем, таблиц и полей;
- динамическую схему БД: поля показываются только для таблиц, релевантных запросу, истории или последним SQL;
- RBAC-проверку SQL для ручного `/sql` и LLM-инициированного `execute_sql`;
- авторазвёртывание `SELECT *` в явный список разрешённых полей при отказе RBAC;
- долгосрочную память пользователя в Redis и интерактивное управление фактами;
- персистенцию состояния сессии (`current_db`, `current_model`, `summary`, `last_sqls`) в Redis;
- аудит вопросов/ответов, SQL-запросов и фактов развёртывания `SELECT *`;
- защиту от параллельных запросов одного пользователя через `state.lock`;
- auto-compact истории при приближении к лимиту контекста.

`execute_template`, `_run_yaml_template`, `analysis_model` и `ANALYSIS_MARKERS` из текущей реализации удалены. `config/templates.yaml` может оставаться в проекте на будущее, но движком больше не используется.

## Константы и `TOOLS_DEFINITION`

- `dv_file_version` — версия файла `261030.01`.
- `TIME_MARKERS` — маркеры временных вопросов: `когда`, `дата`, `время`, `последние`, `последняя`, `свежие`, `newest`, `latest`.
- `STRUCTURE_MARKERS` — маркеры запросов структуры: `описание`, `поля`, `структура`, `метаданные`.
- `_TABLE_TOKEN_STOPWORDS` — служебные токены, которые не считаются упоминанием таблицы при динамическом выборе схемы.
- `TOOLS_DEFINITION` — JSON-схема инструментов для LLM: только `execute_sql` и `get_relations`.

`get_table_schema` в `TOOLS_DEFINITION` намеренно не добавляется: модель видит схему в системном промпте, а `SHOW COLUMNS` не прошёл бы `validate_sql` (разрешены только `SELECT`/`WITH`). `execute_template` также удалён из инструментов.

## Функция `_build_base_prompt`

### `_build_base_prompt(db_type: str, schemas: list[str] | None = None, default_schema: str = "") -> str`

Формирует системный промпт с учётом диалекта БД: `mysql`, `postgresql` или `sqlite`. Содержит:
- ограничения диалекта SQL;
- запрет/ограничение запросов к системным каталогам;
- список переданных схем и схему по умолчанию;
- требование всегда указывать схему перед именем таблицы;
- правила анализа: несколько SQL для сложных вопросов, сравнение с предыдущим периодом, поиск узкого места, предупреждение об устаревших данных;
- требование учитывать долгосрочную память как постоянный фильтр SQL;
- правило идентификаторов для MySQL: имена таблиц, колонок и алиасов оборачиваются в обратные кавычки;
- правила вывода: HTML-формат `<b>`/`<code>`, запрет Markdown-таблиц и Markdown-разметки, краткость, блок «Что делать», запрет лишних упоминаний таблиц/полей;
- обработку отказов доступа: запрещено придумывать результат или обходить запрет;
- требование писать краткий комментарий перед каждым вызовом инструмента.

В `process_message` в `schemas` передаётся список схем из `db_manager.get_schemas(current_db)` без RBAC-фильтрации. RBAC-отфильтрованный блок схем и таблиц отдельно добавляется через `_get_db_schema(state, msg.text)`.

## Класс `RepdixEngine`

Главный диспетчер.

### `__init__(db_manager=None, llm_client=None, cache_manager=None, default_db: str | None = None, user_manager=None)`

Параметры:
- `db_manager` — менеджер БД;
- `llm_client` — клиент LLM;
- `cache_manager` — опциональный Redis/кэш;
- `default_db` — БД по умолчанию;
- `user_manager` — источник ролей пользователя.

Инициализирует `RBACGuard`, `SessionStore`, лимиты и настройки:
- `max_history=10`;
- `tool_model="tool_calling_model"`;
- `show_model_in_response` из `SHOW_MODEL_IN_RESPONSE`;
- `max_rows_in_response` из `MAX_ROWS_IN_RESPONSE`, по умолчанию `100`;
- `max_message_length` из `MAX_MESSAGE_LENGTH`, по умолчанию `4000`;
- `agent_max_steps` из `AGENT_MAX_STEPS`, fallback `10`, минимум `1`; некорректное значение не роняет инициализацию.

`RBACGuard` создаётся с `technical_columns_getter=None`: `RBACGuard.validate_sql` синхронный, а `db_manager` асинхронный. Дополнительная защита от технических колонок выполняется фильтрацией результата в `_format_sql_result()`.

### `process_message(msg: ChatMessage) -> ChatResponse`

Основная точка входа. Проверяет `state.lock`: если запрос уже обрабатывается, возвращает сообщение ожидания. Иначе захватывает lock и гарантированно освобождает его в `finally`.

Порядок обработки:
1. `/help` обрабатывается до загрузки истории.
2. Загружается история из кэша `repdix:history:{user_id}`. Поддерживается новый формат `{"history": [...], "summary": "..."}` и старый формат-список.
3. Загружается долгосрочная память из `repdix:memory:{user_id}`; отсутствующий ключ не сбрасывает факты, уже добавленные в текущей сессии.
4. Загружается `session:{user_id}`: `current_db`, `current_model`, `summary`, `last_sqls`. Восстановленная БД проверяется по `db_manager.get_db_names()`. Битый/отсутствующий ключ не сбрасывает значения, уже установленные в сессии.
5. Пересчитывается `default_schema` уже после восстановления `current_db`.
6. Обрабатываются команды.
7. Обрабатывается детерминированный запрос имени таблицы.
8. Если `llm_client` отсутствует — возвращается `Эхо: <text>`.
9. Иначе строится промпт, проверяется контекст, выбирается модель и запускается LLM-путь с tool-calling циклом.
10. Сохраняется история, пишется аудит, возвращается `ChatResponse`.

#### Команды

- `/help` — читает `config/help.md`; при отсутствии файла возвращает подсказку использовать `/db`, `/model`, `/sql`.
- `/clear` — очищает историю и `summary`, удаляет `repdix:history:{user_id}` и сохраняет сессию. Долгосрочная память сохраняется.
- `/tokens` — оценивает количество сообщений, лимит контекста и примерный размер истории в токенах (`total_chars // 4`).
- `/history` — выводит историю с ролями и обрезкой каждого сообщения до 300 символов.
- `/compact` — сжимает историю через LLM; требует минимум 4 сообщения и подключённый `llm_client`. Сохраняет резюме как `system`-сообщение, обновляет `repdix:history:{user_id}` и вызывает `_persist_session`.
- `/settings` — показывает активную модель, лимит истории, лимит контекста, доступные БД и их display-имена, текущую БД, доступные схемы с учётом RBAC, схему по умолчанию, доступные модели и `SQL_QUERY_TIMEOUT`.
- `/remember <текст>` — добавляет факт в долгосрочную память и сохраняет его в Redis.
- `/memory` — возвращает `ChatResponse` с текстом и `reply_markup` для управления фактами.
- `/forget <номер>` — удаляет факт по 1-based номеру и сохраняет память.
- `/reset` — очищает историю, `summary`, долгосрочную память и `last_sqls`; удаляет ключи истории и памяти; сохраняет сессию.
- `/db <name>` — переключает текущую БД, если имя есть в `get_db_names()`, сохраняет сессию и показывает display-имя и схему по умолчанию.
- `/db` — показывает текущую БД и список доступных БД.
- `/model <name>` — разрешает имя модели через `resolve_model_name()`, фиксирует модель в сессии и сохраняет сессию.
- `/model` — показывает текущую модель или режим авто-роутера.
- `/models` — выводит список доступных моделей.
- `/sql <query>` — ручной SQL:
  - вызывает `validate_sql(sql)`;
  - при наличии `user_manager` выполняет RBAC-проверку;
  - при отказе RBAC по `SELECT *` пробует развернуть звёздочку через `_try_expand_select_star` с `source="manual"`;
  - администратор обходит обычные RBAC-запреты, но не технический фильтр;
  - при успешной неадминской RBAC-проверке подменяет SQL нормализованным `rbac_msg`;
  - требует выбранную БД;
  - для выполнения использует `strip_sql_comments(sql)`, в аудит и `last_sqls` ручной SQL не добавляется;
  - выполняет запрос, форматирует результат через `_format_sql_result`, пишет SQL-аудит с `source="manual"`;
  - автоматический `LIMIT` для ручного `/sql` не добавляется.

#### Детерминированный запрос имени таблицы

Если выбрана БД и текст полностью соответствует `[a-zA-Z0-9_]+`:
- анализирует последние 2 сообщения истории;
- если есть временной контекст (`TIME_MARKERS`) и нет структурного (`STRUCTURE_MARKERS`), пытается получить последнюю дату через `_try_get_last_date`;
- иначе получает структуру таблицы через `db_manager.get_table_schema` и форматирует её через `_format_sql_result`;
- отдельная RBAC-проверка в этой ветке не выполняется.

#### LLM-путь

- `_route_query()` всегда возвращает `state.current_model`, если модель выбрана, иначе `self.tool_model`. Отдельной `analysis_model` больше нет.
- Собирает `base_prompt`, текущую дату, последние 3 SQL, RBAC-отфильтрованную динамическую схему через `_get_db_schema(state, msg.text)`.
- Добавляет долгосрочную память в системный промпт.
- При временных маркерах добавляет подсказку о последней таблице из `state.last_sqls` или истории.
- Проверяет переполнение контекста:
  - при оценке `> 60%` лимита и наличии минимум 4 сообщений выполняет auto-compact первых 4 сообщений через `_compact_history`;
  - при `> 80%` обрезает историю до последних 2 сообщений;
  - если это не помогает — предлагает `/compact` или `/clear`.
- Запускает агентский цикл до `self.agent_max_steps` шагов:
  - перед вызовом инструмента при наличии `status_callback` отправляет статус. HTML-теги из статуса удаляются, пробелы схлопываются: статусы всегда plain text;
  - неизвестные инструменты возвращают ошибку в LLM;
  - `get_relations` читает `config/relations.yaml`, фильтрует технические таблицы/колонки и проверяет RBAC через `validate_sql("SELECT 1 FROM <table>")` при наличии `user_manager`. RBAC для `ref_table` из метаданных не применяется;
  - `execute_sql` валидируется через `validate_sql`, проверяется RBAC, при отказе по `SELECT *` пробует развернуть звёздочку через `_try_expand_select_star` с `source="llm"`, поддерживает admin-bypass только для нетехнических отказов. При успешной неадминской проверке подменяет SQL нормализованным `rbac_msg`;
  - если нет `LIMIT` и запрос не агрегатный, добавляет `LIMIT`: `10` для `text_transcrib`/`dialog_from_whisper`, иначе `max_rows_in_response`;
  - для выполнения использует SQL без комментариев, в аудит пишет оригинал;
  - выполняет запрос, форматирует результат, пишет SQL-аудит с `source="llm"` и сохраняет успешный SQL в `state.last_sqls` (до 20 записей).
- После цикла при отсутствии финального текста и достижении `max_steps` пишет WARNING `Agent loop reached max_steps`, затем просит LLM дать финальный ответ без tools.
- Пустой ответ заменяется предупреждением.
- Длинные ответы с признаками отказа/извинений при длине > 150 символов заменяются на `⛔ Нет доступа к данным.`
- Итоговый текст обрезается по `max_message_length`, из него удаляются псевдо-вызовы `execute_sql(...)`.
- История сохраняется через `_save_history`.

## Долгосрочная память

Память хранится в Redis по ключу `repdix:memory:{user_id}` с TTL 30 дней в виде JSON-списка строк. При отсутствии/сбое Redis факты остаются в `state.long_term_facts` на время жизни процесса.

Методы:
- `_load_memory(user_id, state)` — подгружает факты из Redis, если в состоянии их ещё нет. Нужен для callback-хэндлеров, которые не проходят через `process_message`.
- `_fact_hash(fact)` — первые 8 символов md5 текста факта для `callback_data`.
- `_memory_button_label(fact)` — подпись факта для кнопки: одна строка, не более 30 символов.
- `_build_memory_markup(facts)` — собирает inline-клавиатуру. Удаление: `❌ N. <текст>` с `callback_data=mem_del_<hash>`. Изменение: `✏️ N` с `callback_data=mem_edit_<hash>`. Общие кнопки: `mem_add`, `mem_clear`.
- `render_memory(user_id, state=None)` — возвращает `ChatResponse` с текстом и `reply_markup`; факты экранируются через `html.escape`.
- `memory_get(user_id, idx)` — факт по 0-based индексу.
- `memory_find_by_hash(user_id, fact_hash)` — `(idx, fact)` или `None`.
- `memory_add(user_id, fact)` — добавить факт.
- `memory_update(user_id, idx, fact)` — заменить факт по индексу.
- `memory_delete(user_id, idx)` — удалить факт по индексу.
- `memory_delete_by_hash(user_id, fact_hash)` — удалить по хэшу.
- `memory_update_by_hash(user_id, fact_hash, fact)` — заменить по хэшу.
- `memory_clear(user_id)` — очистить всю память.
- `_save_memory(user_id, facts)` — запись в Redis с TTL 30 дней.

Callback-и памяти привязаны к содержимому факта, а не к индексу: индексы «плывут» после удаления/добавления, и кнопка могла изменить не тот факт.

## Персистенция сессии и история

- `_persist_session(state, user_id)` — сохраняет `current_db`, `current_model`, `summary`, `last_sqls` по ключу `session:{user_id}` с TTL 24 часа. Вызывается из `/db <name>`, `/model <name>`, `/compact`, `/clear`, `/reset`.
- `_save_history(state, user_id, user_text, assistant_text)` — добавляет пару `user/assistant`, обрезает историю до `max_history`, удаляет ведущие `system`-сообщения при обрезке, сохраняет `{"history": [...], "summary": "..."}` в `repdix:history:{user_id}` с TTL 86400.
- LLM-инициированные SQL-запросы хранятся отдельно в `state.last_sqls` (до 20) и не теряются при обрезке истории. Ручной `/sql` в `state.last_sqls` не добавляется.
- В `state.last_sqls` попадает SQL после RBAC-нормализации и авто-`LIMIT`, только при успешном выполнении.

## SELECT *: авторазвёртывание

- `_expand_select_star_sql(sql, state, user_roles, default_schema)` — извлекает таблицы через `rbac.extract_tables`, асинхронно подгружает колонки через `db_manager.get_table_schema`, затем вызывает синхронный `rbac.expand_select_star` с `all_columns_getter`. Возвращает новый SQL или `None`.
- `_try_expand_select_star(sql, state, user_roles, default_schema, source)` — разворачивает `SELECT *`, повторно валидирует результат через RBAC и пишет audit.log:
  - успех: `type=select_star_expanded`;
  - неудача: `log_violation(..., "expand_select_star failed")`;
  - повторный отказ: `log_violation(..., "expand_select_star rejected: ...")`.
- `user_id` для логов берётся через `getattr(state, "user_id", None)`, чтобы не падать на `_FakeState` в тестах.
- Сообщения отказа по `SELECT *` перечисляют разрешённые поля, чтобы LLM видел, как переписать запрос.

## Схема БД и RBAC

- `_get_default_schema(state)` — схема по умолчанию: `""` для SQLite; `database` из конфигурации для MySQL/PostgreSQL; fallback: имя текущей БД для MySQL, иначе `public`.
- `_get_db_type(state)` — `mysql`, `postgresql` или `sqlite`; fallback `mysql`.
- `_format_sql_result(rows, db_name=None, schema=None, table_name=None)` — компактный текстовый вывод без Markdown-таблиц. Фильтрует технические колонки через `is_technical`. Если все колонки технические — возвращает отказ доступа. Ограничивает строки через `max_rows_in_response`, длину текстовых полей до 4000, обычных — до 50, общий размер — через `max_message_length`. Для определения текстовых колонок может вызывать `db_manager.get_table_schema`.
- `_extract_table_from_sql(sql)` — извлекает `(schema, table)` из первого `FROM`; `schema=None`, если схема не указана.
- `_split_qualified(name, default_schema="")` — делит `schema.table`; для короткого имени подставляет `default_schema`.
- `_route_query(text, state)` — возвращает `state.current_model` или `tool_model`.
- `_extract_last_table_from_history(state)` — ищет последнюю таблицу сначала в `state.last_sqls` в обратном порядке, затем в истории; исключает `information_schema` и `sqlite_master`; возвращает `schema.table`, если схема была указана.
- `_quote_ident(name, db_type)` — квотирует идентификатор по диалекту: MySQL — обратные кавычки, PostgreSQL/SQLite — двойные.
- `_try_get_last_date(state, table_name)` — ищет `MAX(<дата>)` по типовым колонкам `created_at`, `updated_at`, `date`, `timestamp`, `inserted_at`. Схема, таблица и колонка квотируются по диалекту; идентификаторы проверяются regex.
- `_get_relations_text(allowed_tables=None)` — читает `config/relations.yaml`, фильтрует технические таблицы/колонки через `is_technical_qualified`, при необходимости фильтрует по списку fully-qualified таблиц.
- `_get_db_schema(state, user_query="")` — формирует блок «Доступные таблицы и поля» с учётом RBAC и динамической фильтрации:
  - получает схемы через `db_manager.get_schemas`;
  - получает таблицы через `db_manager.get_table_list`;
  - для admin разрешает все схемы, wildcard `*`, поля не фильтрует;
  - для остальных использует `rbac.get_allowed_schemas`, `rbac.get_allowed_tables`, `_collect_rules`, `_match_table_rule`;
  - поддерживает wildcard-правила и fully-qualified имена `schema.table`;
  - оставляет таблицы, релевантные вопросу, последним 4 сообщениям истории и последним SQL; если совпадений нет — выводит только список имён таблиц;
  - добавляет отфильтрованные связи таблиц.
- `_filter_target_tables(target, state, user_query)` — оставляет релевантные таблицы. Ищет прямые упоминания и смысловые подсказки (`центр` → `center/centers`, `выручка` → `revenue/appointment/sales` и т.п.). Возвращает `(таблицы, matched)`.
- `_recent_history_text(state)` — текст последних 4 сообщений истории для поиска упоминаний таблиц.
- `_text_mentions_table(text_lower, table)` — проверяет упоминание таблицы полностью или по частям.
- `_describe_table(state, schema, table, rules, all_tables)` — описание одной таблицы для промпта. Находит реальное имя таблицы в `all_tables`, получает колонки через `db_manager.get_table_schema`, применяет `allowed`/`excluded`/`has_wildcard` из RBAC-правила.
- `_compact_history(state, model_name, messages)` — сжимает сообщения в 2 предложения, сохраняет цифры, имена таблиц и условия фильтрации. Возвращает резюме не длиннее 1000 символов или `None` при ошибке.

## Аудит

- `_log_audit(state, msg, question, answer)` — JSON-аудит вопроса/ответа: timestamp, user_id, chat_id, question, answer, db, model, model_used.
- `_log_sql_audit(sql, duration, status, error=None, source="llm")` — SQL-аудит; `type` = `sql_manual` или `sql_llm`, содержит SQL, длительность, статус и ошибку при наличии.
- `_log_star_expansion(original_sql, expanded_sql, source="llm")` — audit.log типа `select_star_expanded`.

## Переменные окружения

- `SHOW_MODEL_IN_RESPONSE` (`true`/`false`) — приписывать ли модель к ответу.
- `MAX_ROWS_IN_RESPONSE` (по умолчанию `100`) — лимит строк в ответе и автоматический `LIMIT` для LLM-SQL.
- `MAX_MESSAGE_LENGTH` (по умолчанию `4000`) — лимит длины ответа и форматированного SQL-результата.
- `SQL_QUERY_TIMEOUT` (по умолчанию `15`) — используется в сообщениях о таймауте SQL.
- `AGENT_MAX_STEPS` (по умолчанию `10`, минимум `1`) — лимит шагов агентского цикла. При достижении лимита пишется WARNING `Agent loop reached max_steps`.

## Зависимости (из импортов)

Стандартная библиотека:
- `asyncio` — импортируется, но в текущей реализации явно не используется;
- `hashlib` — хэш фактов памяти;
- `html` — экранирование фактов в `/memory`;
- `json`, `logging`, `os`, `re`, `time`;
- `datetime` из `datetime`.

Сторонние:
- `yaml` (PyYAML) — чтение `config/relations.yaml`. `config/templates.yaml` движком больше не используется.

Внутрипроектные:
- `app.core.session`: `SessionState`, `SessionStore` — состояние сессии и хранилище.
- `app.core.technical_filter`: `is_technical`, `is_technical_qualified` — проверка технических таблиц/колонок.
- `app.core.validator`: `validate_sql`, `log_violation`, `strip_sql_comments` — проверка SQL, логирование нарушений, удаление комментариев перед выполнением.
- `app.core.rbac_guard`: `RBACGuard` — RBAC-проверки и развёртывание `SELECT *`.
- `app.interfaces.base`: `ChatMessage`, `ChatResponse` — DTO на входе/выходе.

Файлы конфигурации:
- `config/relations.yaml` — связи таблиц для `get_relations` и подсказок в промпте;
- `config/help.md` — текст `/help`.

Ожидаемые интерфейсы:
- `db_manager`: `get_db_config`, `get_db_type`, `get_db_names`, `get_db_display_name`, `get_schemas`, `get_table_list`, `get_table_schema`, `execute_query`.
- `llm_client`: `chat(messages, tools=..., model_name=..., tool_choice=...)`, `get_context_length(model)`, `get_model_names()`, `resolve_model_name(name)`.
- `cache_manager`: `get`, `set`, `delete`.
- `user_manager`: `get_user_roles(user_id)`.
- `RBACGuard`: `validate_sql`, `get_allowed_schemas`, `get_allowed_tables`, `_load_config`, `_collect_rules`, `_match_table_rule`, `extract_tables`, `expand_select_star`, `is_select_star_denial`.

## Использование другими модулями проекта

1. **Адаптеры мессенджеров** создают `ChatMessage` с полями `user_id`, `chat_id`, `text`, `status_callback` и вызывают `RepdixEngine.process_message()`, получая `ChatResponse(text=..., reply_markup=...)`. `status_callback` используется для промежуточных статусов.
2. **Точка сборки приложения** обычно конструирует `RepdixEngine(db_manager=..., llm_client=..., cache_manager=..., user_manager=..., default_db=...)`.
3. **Callback-хэндлеры адаптера** могут вызывать `render_memory`, `memory_get`, `memory_find_by_hash`, `memory_add`, `memory_update`, `memory_delete`, `memory_delete_by_hash`, `memory_update_by_hash`, `memory_clear`, не заходя в `process_message`.
4. **`app.core.session`** предоставляет `SessionStore` и `SessionState` с полями `history`, `last_sqls`, `current_db`, `current_model`, `last_model_used`, `summary`, `long_term_facts`, `lock`.
5. **`app.core.validator` / `app.core.rbac_guard`** вызываются напрямую для валидации SQL, RBAC-проверок и развёртывания `SELECT *`. `_get_db_schema` обращается к полуприватным методам `rbac._load_config`, `rbac._collect_rules`, `rbac._match_table_rule`.
6. **`db_manager`** используется для получения конфигурации БД, списка БД, схем, таблиц, структуры таблиц и выполнения запросов.
7. **`llm_client`** используется для чата, оценки контекста, списка моделей и разрешения имени модели.
8. **`cache_manager`** используется с ключами:
   - `repdix:history:{user_id}` — история и `summary`, TTL `86400`;
   - `repdix:memory:{user_id}` — долгосрочная память, TTL `30 * 86400`;
   - `session:{user_id}` — `current_db`, `current_model`, `summary`, `last_sqls`, TTL `86400`.
9. **`user_manager`** предоставляет роли. Роль `admin` обходит обычные RBAC-запреты, но не технический фильтр: технические таблицы/колонки запрещены всем ролям.

## Заметки и ограничения

- **`get_table_schema` не входит в `TOOLS_DEFINITION`** намеренно: модель видит схему в системном промпте, а `SHOW COLUMNS` не прошёл бы `validate_sql`.
- **RBAC для `/sql` и LLM `execute_sql`**: при успехе неадминской проверки `validate_sql` возвращает нормализованный SQL, который подменяет исходный (`sql = rbac_msg`). Для admin-bypass SQL остаётся как есть.
- **Админ и технический фильтр**: администратор обходит только обычные RBAC-правила, но не технический фильтр. Признак технического отказа определяется по подстроке `technical` в сообщении RBAC.
- **`SELECT *`**: при отказе RBAC движок пытается развернуть звёздочку в разрешённые поля, повторно валидирует результат и пишет аудит. Неудачи и повторные отказы фиксируются через `log_violation`.
- **RBAC-проверки в `process_message`** для `/sql` и `execute_sql` выполняются только при наличии `user_manager`. `_get_db_schema` при этом всё равно использует `RBACGuard`.
- **`get_relations` tool-loop**: обработчик читает `config/relations.yaml`, фильтрует технические таблицы/колонки и проверяет RBAC для запрошенной таблицы через `validate_sql`. RBAC для `ref_table` из метаданных не применяется.
- **Детерминированная ветка по имени таблицы** не вызывает `validate_sql`/`RBACGuard` и доступна при выбранной БД; она возвращает структуру таблицы или последнюю дату.
- **`_build_base_prompt`** получает список схем из `db_manager.get_schemas(current_db)` без RBAC-фильтрации; RBAC-отфильтрованные схемы и таблицы добавляются отдельно через `_get_db_schema`.
- **История из кэша** загружается в начале `process_message` при наличии `cache_manager`; при наличии кэша она перезаписывает `state.history`.
- **Сессия из Redis**: `session:{user_id}` читается при входе в `process_message`. Битый/отсутствующий ключ не сбрасывает значения, уже установленные в сессии. БД из сессии не восстанавливается, если её больше нет в `db_manager.get_db_names()`.
- **`default_schema` пересчитывается после загрузки сессии**; для MySQL это имя базы, а не всегда `public`.
- **Auto-compact** срабатывает при оценке `> 60%` лимита контекста и наличии минимум 4 сообщений. Резюме дублируется в `state.summary` и сохраняется вместе с историей.
- **Защита контекста**: при `> 80%` лимита история обрезается до 2 сообщений; если не помогает — предлагается `/compact` или `/clear`.
- **`analysis_model`** удалена: `_route_query` всегда возвращает выбранную пользователем модель или `tool_model`. `ANALYSIS_MARKERS` в коде отсутствует.
- **`execute_template`** и `_run_yaml_template` удалены; `config/templates.yaml` больше не используется движком.
- **Автоматический `LIMIT`**: для LLM-SQL добавляется, если в запросе нет `LIMIT` и нет `GROUP BY` или агрегатной функции (`COUNT/SUM/MIN/MAX/AVG`). Для `text_transcrib`/`dialog_from_whisper` — `10`, иначе `max_rows_in_response`.
- **Обрезка ответа** выполняется по `self.max_message_length`, а не по жёсткому `2000`.
- **Статусы** очищаются от HTML-тегов и схлопываются в одну строку: Telegram не принимает произвольные теги в статусах.
- **Кнопки памяти** используют `callback_data` на основе md5-хэша содержимого факта, а не индекса. Подпись кнопки ограничена 30 символами.
- **Логовые каналы**: `logger` — общий, `audit_logger` — структурированный JSON-аудит; типы SQL-аудита `sql_manual`, `sql_llm`, дополнительно `select_star_expanded`.
- **Технические колонки**: `_format_sql_result` дополнительно фильтрует их из результата как защита в глубину. Если после фильтрации не осталось колонок, возвращается отказ доступа, а не «пусто».
- **`asyncio`** импортируется, но в текущей реализации явно не используется.