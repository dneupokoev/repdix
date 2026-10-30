# 📘 Проект: repdix (Reporting & Data eXplorer)

**💡 AI-аналитик** — Telegram-бот-оркестратор аналитики: переводит вопросы на естественном языке в SQL и возвращает глубокий аналитический ответ.

## 🎯 1. Цель проекта
Создать универсального Telegram-бота-оркестратора с системой модерации доступа, который принимает вопросы на естественном языке, переводит их в SQL-запросы через LLM, выполняет их в базе данных (MySQL, PostgreSQL, SQLite) и возвращает пользователю **глубокий аналитический ответ**.

Бот работает в режиме **AI-агента**: он умеет самостоятельно строить цепочки SQL-запросов (Agent Loop), находить скрытые закономерности (например, «почему упали продажи»), показывать процесс своих рассуждений в реальном времени (Chain of Thought) и прерывать выполнение по команде `/stop`.

## 🛠️ 2. Технологический стек
- **Язык:** Python 3.12.
- **Менеджер зависимостей:** Pipenv.
- **Асинхронность:** `asyncio`, `asyncio.Task` (для фоновых задач и отмены).
- **Telegram:** `aiogram` (v3.x) + `FSMContext` (для интервью при регистрации и редактирования памяти).
- **LLM:** `openai` (AsyncOpenAI). Основная модель — **DeepSeek V4.1 Flash** (через OpenCode Zen) или локальные аналоги.
- **Базы данных:** `aiomysql` (MySQL), `asyncpg` (PostgreSQL), `aiosqlite` (SQLite).
- **Кэширование:** `redis.asyncio`.
- **Конфигурация:** `python-dotenv` и `PyYAML`.
- **Парсер SQL:** `sqlglot` (диалект `mysql` для RBAC-разбора; диалекты `mysql`/`postgres`/`sqlite` для проверки сложности).

## 🏗️ 3. Архитектура (Порты и Адаптеры)
Ядро ничего не знает о Telegram. Общение идёт через абстрактные интерфейсы.

```text
repdix/
├── app/
│   ├── __init__.py
│   ├── main.py                 # точка входа: логирование, инициализация сервисов,
│   │                           # загрузка .env до выбора адаптера, предсказуемый выбор
│   │                           # адаптера (telegram/cli), диагностика выбора, graceful shutdown
│   ├── core/                   # бизнес-логика (независима от мессенджера)
│   │   ├── __init__.py
│   │   ├── cache_manager.py    # Redis (история диалогов, память, session:{user_id});
│   │   │                       # клиент без retry_on_timeout; fail-fast при открытом
│   │   │                       # Redis на не-loopback хосте без пароля (CVE-2025-21605)
│   │   ├── db_manager.py       # пулы соединений (MySQL/PostgreSQL/SQLite), таймауты,
│   │   │                       # read-only, init_statements, метаданные, multi-schema,
│   │   │                       # lock_wait_timeout для MySQL/MariaDB,
│   │   │                       # выполнение запросов без SQL-комментариев;
│   │   │                       # CLIENT.MULTI_STATEMENTS — только при ';' в init_cmd
│   │   ├── engine.py           # ядро: Agent Loop, tool_calls, RBAC, формирование промпта
│   │   │                       # (включая правило обрамления идентификаторов MySQL),
│   │   │                       # динамическая схема БД, авто-сжатие истории,
│   │   │                       # авто-развёртывание SELECT *, безопасное чтение
│   │   │                       # user_id/current_db через getattr,
│   │   │                       # вызов check_complexity перед выполнением SQL,
│   │   │                       # _extract_table_from_sql / _extract_last_table_from_history
│   │   │                       # на sqlglot
│   │   ├── llm_client.py       # клиент LLM (hot-reload models.yaml, temperature,
│   │   │                       # max_tokens, fallback tools/tool_choice, orphaned clients)
│   │   ├── manual_sql_audit.py # аудит ручных /sql-вызовов в logs/manual_sql.log
│   │   ├── rbac_guard.py       # проверка прав доступа к таблицам и полям (sqlglot AST),
│   │   │                       # правила schema.*, защита от P2SQL, expand_select_star,
│   │   │                       # COUNT(*) не блокируется
│   │   ├── session.py          # SessionState (история, lock, current_task, last_sqls,
│   │   │                       # summary, long_term_facts) + SessionStore с TTL
│   │   │                       # и фоновой очисткой (start_cleanup_task/stop_cleanup_task)
│   │   ├── sql_complexity.py   # проверка сложности SQL на уровне AST (JOIN, декартово
│   │   │                       # произведение, глубина подзапросов, UNION); пороги из env
│   │   ├── technical_filter.py # фильтрация технических таблиц/колонок (TECHNICAL_PREFIXES)
│   │   ├── user_manager.py     # управление доступом (hot-reload users.yaml),
│   │   │                       # add/approve/reject/delete, защита env-админов,
│   │   │                       # атомарная запись с fsync
│   │   └── validator.py        # SQL-валидатор (только SELECT/WITH, forbidden keywords,
│   │                           # fail-closed, комментарии без пробела после --,
│   │                           # DANGEROUS_PATTERNS только вне литералов)
│   └── interfaces/             # адаптеры мессенджеров
│       ├── __init__.py
│       ├── base.py             # ChatMessage (с колбэком статуса), ChatResponse
│       │                       # (с reply_markup), BaseChatInterface
│       ├── telegram_adapter.py # Telegram (aiogram), FSM-регистрация, HTML-разметка,
│       │                       # санитайзер HTML, кнопки управления памятью, /stop,
│       │                       # модерация, админ-панель /users (только env-админы),
│       │                       # нарезка длинных сообщений не внутри HTML-тегов
│       └── cli_adapter.py      # Консольный интерфейс (отладка, CLI_USER_ID)
├── config/
│   ├── databases.yaml          # шаблоны подключений к целевым БД (${VAR} из .env)
│   ├── models.yaml             # шаблоны моделей LLM (temperature, context_length,
│   │                           # max_tokens, supports_tools)
│   ├── relations.yaml          # описание связей (для JOIN)
│   ├── templates.yaml          # детерминированные шаблоны (топы/рейтинги, только MySQL)
│   ├── users.yaml              # список пользователей, ролей и статусов (НЕ КОММИТИТЬ)
│   ├── roles.yaml              # описание ролей и прав (RBAC)
│   └── help.md                 # справка /help
├── data/                       # локальные данные (опционально)
├── tests/                      # pytest (16 файлов, 183 теста)
├── logs/                       # audit.log, security.log, manual_sql.log (ротация, НЕ КОММИТИТЬ)
├── .env                        # секреты (НЕ КОММИТИТЬ)
└── .gitignore
```

### 3.1. Ключевые модули
- `app/main.py` — запуск приложения, инициализация сервисов, загрузка `.env` до выбора адаптера, предсказуемый выбор адаптера (`telegram` или `cli`), диагностика выбора в логах, корректное завершение (`_shutdown`: остановка cleanup-таска, отмена сессий, закрытие пулов БД/LLM/Redis/адаптера). `_env_int` — безопасный парсинг `LOG_MAX_BYTES`/`LOG_BACKUP_COUNT`/`SECURITY_LOG_*`.
- `app/core/engine.py` — Agent Loop, вызовы инструментов, проверки RBAC, вызов `sql_complexity.check_complexity` перед выполнением SQL, сборка системного промпта, динамическая схема БД, авто-сжатие истории, управление памятью, авто-развёртывание `SELECT *` в разрешённые поля, безопасное чтение `user_id` и `current_db` через `getattr`. `_format_sql_result` кэширует набор текстовых колонок (`_text_columns_cache`, ключ `(db, schema, table)`), чтобы не запрашивать `information_schema` на каждый вызов. **Важно:** `/sql` не проходит `check_complexity` (осознанное решение владельца). `_extract_table_from_sql` и `_extract_last_table_from_history` переведены на `sqlglot` (кавычки, CTE, подзапросы; системные таблицы пропускаются).
- `app/core/session.py` — состояние сессии, история, блокировки, активные задачи, резюме диалога (`summary`), долгосрочная память (`long_term_facts`) и TTL-хранилище сессий. **Фоновая очистка** (`start_cleanup_task`/`stop_cleanup_task`, интервал 300 сек) — иначе при малом числе пользователей просроченные сессии жили бы до рестарта. Ленивая очистка `_cleanup_expired` раз в 100 обращений к `get()` сохранена как дополнительный механизм.
- `app/core/db_manager.py` — пулы подключений к MySQL/PostgreSQL/SQLite, таймауты, `lock_wait_timeout` (MySQL/MariaDB), read-only-режим, `init_statements`, кэш метаданных и поддержка нескольких схем (`multi-schema`). Перед отправкой запроса в БД SQL-комментарии вырезаются. **Важно:** `CLIENT.MULTI_STATEMENTS` включается **только если `init_cmd` содержит `;`** (несколько `SET SESSION`) — иначе флаг `0`, чтобы не расширять поверхность атаки; `init_statements` склеиваются через `; `; при наличии `;` вне литералов пишется WARNING. `get_schemas(auto)` при сбое возвращает `[]` (а не fallback-схему) — чтобы LLM не получила ложную картину. `execute_query` (MySQL) содержит комментарий: `asyncio.wait_for` не отменяет серверный запрос, основная защита — `MAX_EXECUTION_TIME`.
- `app/core/llm_client.py` — клиент LLM, hot-reload `models.yaml`, `max_tokens`, `temperature`, fallback на `tool_choice`/`tools` при ошибках API. При hot-reload, если event loop закрыт, старый клиент попадает в `_orphaned_clients` и закрывается в `close()` (иначе утечка соединений).
- `app/core/cache_manager.py` — Redis-кэш истории диалогов, памяти и `session:{user_id}`, graceful degradation при недоступности Redis. Redis-клиент создаётся без `retry_on_timeout`. **Fail-fast:** открытый в сеть Redis без пароля на не-loopback хосте запрещён (CVE-2025-21605).
- `app/core/rbac_guard.py` — контроль доступа к таблицам и полям на основе AST `sqlglot`, правила вида `schema.*`, блокировка системных таблиц (`information_schema`, `pg_catalog`, `sqlite_master`) в любом месте AST, блокировка `SELECT *` при наличии ограничений, метод `expand_select_star` для авто-развёртывания звёздочки в разрешённые поля. **Порядок проверки:** точное `schema.table` → `*` → `schema.*` → короткое имя (только если схема не указана). Для неквалифицированных колонок при нескольких таблицах — требование «разрешена во ВСЕХ таблицах» (`all`, не `any`). `COUNT(*)` не блокируется правилом `excluded_columns`/`allowed_columns` (агрегат не возвращает поля).
- `app/core/sql_complexity.py` — проверка сложности SQL на уровне AST: количество `JOIN`, наличие `JOIN` без условия (декартово произведение), максимальная глубина вложенности `Subquery`, количество `UNION`. Пороги задаются через env (`SQL_MAX_JOINS`, `SQL_MAX_SUBQUERY_DEPTH`, `SQL_MAX_UNIONS`). При превышении — `SQLTooComplex`, запрос не выполняется, модели возвращается tool-сообщение с причиной. Диалекты: `mysql` (MySQL/MariaDB), `postgres` (PostgreSQL), `sqlite`; `postgresql` нормализуется в `postgres`.
- `app/core/technical_filter.py` — скрытие технических таблиц и колонок по префиксам из `TECHNICAL_PREFIXES`. Техничным считается имя, у которого **любая** часть (разделённая точками) начинается с префикса: `technical_schema.users`, `db.technical_logs`, `db.technical_foo.bar`.
- `app/core/validator.py` — проверка SQL: только `SELECT`/`WITH`, запрещённые ключевые слова, опасные паттерны, fail-closed парсинг через `sqlglot`, корректная обработка SQL-комментариев без пробела после `--`, отказ при `;` вне строковых литералов. **DANGEROUS_PATTERNS ищутся только вне строковых литералов** (`_strip_string_literals`): `SELECT 'SET GLOBAL x'` разрешён, `SET GLOBAL` как команда — нет.
- `app/core/user_manager.py` — модерация пользователей, hot-reload `users.yaml`, защита администраторов (env-админы не перезаписываются и не удаляются), атомарная запись через `os.replace` с `fsync` (защита от потери `users.yaml` при падении питания).
- `app/core/manual_sql_audit.py` — аудит ручных `/sql`-вызовов в отдельный `logs/manual_sql.log` (полный SQL, `user_id`, `db_name`, длительность, результат). Ограничений на сложность `/sql` нет (осознанное решение владельца). Ротация: `LOG_MAX_BYTES`/`LOG_BACKUP_COUNT`. При ошибке открытия файла — graceful degradation (WARNING, аудит отключён).
- `app/interfaces/base.py` — абстрактные интерфейсы сообщений (`ChatMessage`) и ответов (`ChatResponse`, включая `reply_markup`).
- `app/interfaces/telegram_adapter.py` — Telegram-адаптер: FSM-регистрация, HTML-разметка ответов (`parse_mode`), санитайзер HTML (`sanitize_telegram_html`: безопасные теги сохраняются, опасные экранируются; для `<a>` разрешены только `http(s)://` и `tg://`), отправка статусов с HTML-разметкой, кнопки управления памятью, `/stop`, модерация, админ-панель `/users` (только env-админы). **Нарезка длинных сообщений** (`_split_long_text`) не попадает внутрь HTML-тега `<...>` (иначе Telegram отклоняет разметку).
- `app/interfaces/cli_adapter.py` — консольный адаптер для отладки. Предупреждает, если `CLI_USER_ID` не админ (RBAC заблокирует таблицы). `Ctrl+C` отменяет текущий запрос (аналог `/stop`; в CLI сам `/stop` уходит в LLM — используйте `Ctrl+C`).

### 3.2. Поток обработки запроса
1. Адаптер (`telegram_adapter.py` или `cli_adapter.py`) принимает сообщение пользователя и передаёт его в ядро.
2. `engine.py` формирует системный промпт с учётом динамической схемы БД, истории, долгосрочной памяти и RBAC-правил.
3. LLM вызывает инструменты (`execute_sql`, `get_relations`), ядро валидирует SQL через `validator.py`, проверяет доступ через `rbac_guard.py`, проверяет сложность через `sql_complexity.py` и скрывает технические объекты через `technical_filter.py`. При отказе RBAC именно из-за `SELECT *` (при наличии `allowed_columns`/`excluded_columns`) ядро пытается развернуть звёздочку в разрешённые поля и повторить валидацию. При отказе `check_complexity` запрос **не выполняется**, модели возвращается tool-сообщение с причиной, чтобы она переформулировала запрос или попросила пользователя уточнить вопрос.
4. `db_manager.py` выполняет запрос в целевой БД с учётом таймаутов, `lock_wait_timeout` и read-only-режима. Перед отправкой запроса SQL-комментарии вырезаются (`strip_sql_comments`); в аудит и `last_sqls` пишется оригинал.
5. Статусы Chain of Thought отправляются через `status_callback`; ответ пользователю рендерится адаптером (в Telegram — с `parse_mode=HTML`, после `sanitize_telegram_html`).

## 🗄️ 4. Работа с базами данных
- **Шаблоны в `config/databases.yaml`**: реальные значения подставляются из `.env` через `${VAR}`.
- **Ограничения на стороне СУБД.** Бот должен работать под пользователем БД, у которого есть **только права `SELECT`**. `readonly: true`, валидатор SQL и проверка сложности — дополнительные барьеры, но **не замена** правам на уровне БД (см. раздел 4.1).
- **SQL-комментарии:** перед отправкой запроса в БД комментарии вырезаются (`strip_sql_comments` — только вне строковых литералов); валидатор при этом корректно обрабатывает SQL-комментарии, в том числе без пробела после `--`.
- **Таймауты:** `SQL_QUERY_TIMEOUT` (по умолчанию 15 секунд) прерывает тяжёлые запросы:
  - **MySQL:** `SET SESSION MAX_EXECUTION_TIME` (только MySQL 5.7.8+, не MariaDB). Ставится через `init_command` при создании пула — то есть на каждое соединение, **в том числе при `readonly: true`** (несколько простых `SET SESSION` склеиваются в один запрос).
  - **MariaDB:** вместо `MAX_EXECUTION_TIME` бот автоматически выставляет `SET SESSION max_statement_time` (в секундах). Для этого в `databases.yaml` нужно указать `is_mariadb: true`.
  - **PostgreSQL:** `statement_timeout` в `server_settings` (прерывает запрос на стороне сервера), плюс `command_timeout` в пуле.
  - **SQLite:** `asyncio.wait_for` вокруг `cursor.fetchall()`.
  - **Клиентская страховка:** для MySQL — `asyncio.wait_for(timeout=SQL_QUERY_TIMEOUT+2)` вокруг выполнения. Это **не отменяет** серверный запрос (aiomysql не шлёт `KILL QUERY`), а лишь снимает ожидание в Python; серверный таймаут (`MAX_EXECUTION_TIME`) остаётся основной защитой.
- **Ожидание блокировок (MySQL/MariaDB):** `MAX_EXECUTION_TIME` и `max_statement_time` **не запускают таймер**, пока запрос ждёт блокировку. Поэтому в тот же `init_command` добавляются:
  - `SET SESSION innodb_lock_wait_timeout = <LOCK_WAIT_TIMEOUT>` — ограничивает ожидание InnoDB row-level lock;
  - `SET SESSION lock_wait_timeout = <LOCK_WAIT_TIMEOUT>` — ограничивает ожидание metadata lock (MDL).
  Значение `LOCK_WAIT_TIMEOUT` берётся из env (по умолчанию 5 секунд). Если значение <= 0 — используется 5 и пишется warning в лог. Для PostgreSQL/SQLite эти команды не добавляются.
- **Режим транзакций read-only (выставляется при `readonly: true`):**
  - **MySQL:** `SET SESSION transaction_read_only = 1` (при `readonly: true`, включая MariaDB). Серверный таймаут и `lock_wait_timeout` выставляются в том же `init_command`: несколько `SET SESSION` склеиваются через `; `. `CLIENT.MULTI_STATEMENTS` включается **только если `init_cmd` содержит `;`** (то есть когда `SET SESSION` действительно несколько) — иначе `client_flag=0`, чтобы не расширять поверхность атаки. Пользователь БД с правами только `SELECT` по-прежнему рекомендуется как основной барьер.
  - **PostgreSQL:** `default_transaction_read_only = on` в `server_settings`.
  - **SQLite:** `PRAGMA query_only = ON`.
- **MariaDB:** установите `is_mariadb: true` в `databases.yaml`. Бот не будет ставить `MAX_EXECUTION_TIME` (MariaDB его не понимает) и вместо него выполнит `SET SESSION max_statement_time = <SQL_QUERY_TIMEOUT>`. `innodb_lock_wait_timeout` и `lock_wait_timeout` ставятся и для MariaDB.
- **Multi-schema:** одна MySQL-БД может содержать несколько схем. Список задаётся в `databases.yaml` (`schemas: auto` или явный список), исключения — `exclude_schemas`. Результат кэшируется на `SCHEMA_CACHE_TTL_SEC` (по умолчанию 3600 сек). Таблицы в промпте и в `get_table_list()` возвращаются fully-qualified (`schema.table`); `get_table_schema()` принимает `(db, table, schema)`. Для `schemas: auto` поддерживается только MySQL; для PostgreSQL/SQLite используется fallback `[conf["database"]]` (или `["main"]` для SQLite). **При сбое `schemas: auto` возвращается `[]`** (а не fallback-схема): LLM увидит «—» и не будет генерировать SQL по несуществующей таблице. Пустой список **не кэшируется** — при восстановлении БД следующий вызов снова опросит `information_schema`.
- **init_statements:** необязательный список SQL-команд в `databases.yaml`, которые выполняются при создании **каждого** соединения MySQL-пула. Поддерживается подстановка `{timeout}` (= `SQL_QUERY_TIMEOUT`, сек). Readonly, таймаут и `lock_wait_timeout` бот выставляет сам; `init_statements` — только для дополнительных настроек сессии. «Простые» `SET SESSION` склеиваются в один запрос через `; `. `CLIENT.MULTI_STATEMENTS` включается **только если итоговый `init_cmd` содержит `;`** (то есть когда команд действительно несколько) — иначе флаг `0`, чтобы не расширять поверхность атаки. **Ограничение:** не используйте `;` внутри строковых литералов — `MULTI_STATEMENTS` разорвёт команду. Бот пишет WARNING, если `;` встречается вне литералов (это может быть намеренно — несколько `SET SESSION` — но чаще ошибка).

### 4.1. 🔐 Безопасность базы данных

⚠️ **ВАЖНО:** Для безопасности бот должен подключаться к базе данных под пользователем, у которого есть **ТОЛЬКО права на `SELECT` (чтение)**. Параметр `readonly: true` в `databases.yaml` **не заменяет** права БД, он лишь выставляет режим транзакций (`SET SESSION transaction_read_only = 1`, `default_transaction_read_only = on`, `PRAGMA query_only = ON`). Если у пользователя БД есть права на запись, скомпрометированная LLM может изменить данные.

Валидатор SQL (`validate_sql`), RBAC-гард и проверка сложности (`check_complexity`) — лишь дополнительный барьер; они не заменяют ограничения прав на стороне СУБД и не гарантируют неизменность данных.

**Рекомендация:** при старте проверять `SHOW GRANTS` (MySQL) и логировать WARNING, если у пользователя БД есть права на запись. В текущей версии это **не реализовано** — ответственность на админе.

### 4.2. 🩺 Диагностика тяжёлых запросов (`slow_query_log`) — на стороне админа БД

`MAX_EXECUTION_TIME`, `max_statement_time`, `statement_timeout` и `lock_wait_timeout` — это **защита**, но она не всегда срабатывает (например, запрос может «проскочить» проверку таймера в некоторых фазах выполнения). Для **диагностики** инцидентов админу БД рекомендуется включить `slow_query_log`:

**MySQL:**
```sql
SET GLOBAL slow_query_log = 'ON';
SET GLOBAL long_query_time = 30;          -- порог в секундах
SET GLOBAL slow_query_log_file = '/var/log/mysql/slow.log';
-- Опционально: логировать и запросы без индексов
SET GLOBAL log_queries_not_using_indexes = 'ON';
```

**MariaDB:** аналогично, но имя файла может отличаться в зависимости от сборки; проверьте `SHOW VARIABLES LIKE 'slow_query_log_file';`.

**Рекомендации:**
- Подключить бота под **отдельным пользователем БД** (например, `repdix_ro`), чтобы в `slow.log` было видно `user: repdix_ro` и алерт срабатывал именно на запросы бота, а не на всю БД.
- Настроить внешний алерт (например, `pt-query-digest` раз в 5 минут или простой скрипт, который парсит `slow.log` и шлёт уведомление при `Query_time > 30`).
- **`slow_query_log` — это диагностика, а не защита.** Он не остановит запрос. Он нужен, чтобы **после** инцидента понять, что произошло.

## 🧠 5. Работа с моделями и AI-Агент

### 5.1. Agent Loop (Цикл агента)
В `engine.py` реализован цикл `while step < max_steps`. Лимит шагов задаётся переменной окружения `AGENT_MAX_STEPS` (по умолчанию 10, минимум 1): пяти шагов не хватало сложным вопросам (несколько SQL подряд + финальный вывод). Модель может: изучить структуру таблиц (из системного промпта), выполнить несколько SQL-запросов, сравнить результаты и написать бизнес-вывод. Если цикл оборвался по лимиту и модель так и не вернула текст, в лог пишется `WARNING "Agent loop reached max_steps"` с полями `steps`, `max_steps`, `user_id`; затем модели отправляется system-сообщение с требованием написать финальный ответ (без инструментов).
**Важно:** В цикле сообщения модели обязательно передаются с ролью `"tool"` и `tool_call_id`, иначе DeepSeek вернёт ошибку 400.

В системный промпт добавлено правило обрамления идентификаторов MySQL обратными кавычками. При формировании простых ответов имена таблиц скрываются от пользователя. Для экономии токенов в `config/models.yaml` можно задать `max_tokens` (лимит длины ответа).

**Неизвестные инструменты:** если LLM вернула `tool_call` с именем, отличным от `execute_sql`/`get_relations`, ядро логирует WARNING и возвращает модели tool-сообщение `Ошибка: неизвестный инструмент '<name>'` (без выполнения).

### 5.2. Chain of Thought (Прозрачность)
Перед каждым вызовом инструмента модель пишет краткое пояснение.
**Как это работает:** В `ChatMessage` есть `status_callback`. `telegram_adapter.py` отправляет эти статусы пользователю (вида `🤖 Сейчас посмотрю структуру...`). В `engine.py` текст перехватывается и отправляется через колбэк. Автоматическая обрезка статусов в ядре удалена (метод `_shorten_text` больше не используется), поэтому статусы отправляются как есть; HTML-разметка в Telegram сохраняется после `sanitize_telegram_html`. `cli_adapter.py` печатает статусы с отступом. Ответы Telegram отправляются с `parse_mode=HTML`.

**Важно:** если модель вернула `tool_calls`, но пустой `content`, статус формируется как `🤖 Выполняю <tool_name>...`. Если `content` непустой — он очищается от HTML-тегов и схлопывается в одну строку (статусы — plain text).

### 5.3. Инструменты модели (Tool Calling)
Доступны инструменты:
- `execute_sql` — для сложных аналитических SELECT-запросов. Перед выполнением проходит `validator.validate_sql`, `rbac_guard.validate_sql` и `sql_complexity.check_complexity`.
- `get_relations` — узнать связи таблиц. Проходит проверку `is_technical_qualified` для имени таблицы и RBAC через `validate_sql("SELECT 1 FROM <table>")`. Связи фильтруются: технические колонки и таблицы-цели (`ref_table`) скрываются.
- **⚠️ Важно:** `SHOW COLUMNS` и `SHOW TABLES` **не пройдут** `validator.validate_sql` — только `SELECT` и `WITH`. Вся нужная информация о структуре уже в промпте (динамическая схема).

### 5.4. Управление историей, контекстом и долгосрочной памятью
- **Динамическая схема БД:** в промпт передаются поля только для тех таблиц, которые упомянуты в вопросе, последних 4 сообщениях истории или последних SQL. Если совпадений нет — передаётся только список имён таблиц (экономия контекста). Поиск таблиц: прямое упоминание (полное имя или сегменты через `_`), смысловые подсказки (`центр` → `center`/`centers`, `выручк` → `revenue`/`appointment`/`sales` и т.п. — см. `_TABLE_KEYWORD_HINTS`).
- **Auto-Compact:** при достижении 60% лимита контекста первые 4 сообщения истории сжимаются LLM в 2 предложения и заменяются одним `system`-сообщением (резюме сохраняется в `state.summary` и в Redis).
- **Жёсткая обрезка:** при > 80% лимита история обрезается до 2 последних сообщений; при повторном переполнении — ошибка с просьбой использовать `/compact` или `/clear`.
- **Долгосрочная память:** пользователь может сохранять факты (`/remember`, кнопка «Добавить») в Redis (TTL 30 дней). Факты добавляются в системный промпт, и LLM обязана учитывать их как постоянные фильтры (например, «работаю только с центром X»).
- **Управление памятью:** команда `/memory` выводит список фактов с Inline-кнопками (Изменить, Удалить, Добавить, Очистить всё). Callback'и привязаны к хэшу текста факта (`md5[:8]`), а не к индексу, чтобы избежать смещения при удалении. Подпись кнопки обрезается до 30 символов.
- **`/clear`** — очищает только историю диалога (долгосрочная память сохраняется).
- **`/reset`** — полный сброс: история, память, `last_sqls`.
- **`/compact`** — принудительное сжатие истории; при недоступности LLM-сжатия используется fallback (ошибка сжимается в текст, история не теряется).
- **`/stop`** — прерывание текущего анализа (отмена `asyncio.Task`). Задачи, запущенные **после** рестарта, отменяются нормально. Задачи, запущенные **до** рестарта, не «докручиваются»: объекты `asyncio.Lock` и `current_task` не сериализуются в Redis (подробнее — раздел 9). В CLI сам `/stop` уходит в LLM — используйте `Ctrl+C` (аналог `/stop`).

## 🛡️ 6. Управление доступом (Модерация и RBAC)
Доступ к боту строго контролируется:
1. **Администраторы** (указаны в `ADMIN_USERS` в `.env` или имеют роль `admin` в `users.yaml`) имеют доступ ко всему. При старте `user_manager.sync_admins()` гарантирует, что все ID из `.env` имеют `is_active: true` и `roles: ["admin"]`.
2. **Обычные пользователи** при первом `/start` проходят интервью (ФИО, Отдел, Цель). Данные сохраняются в `config/users.yaml` (с флагом `is_active: false`). Пока админ не одобрит заявку, бот не пустит пользователя к LLM. **Файл перечитывается «на лету» (hot-reload по mtime) при каждом запросе, перезапуск не нужен.**
3. **Модерация:** админ получает уведомление с кнопками «✅ Одобрить» / «❌ Отклонить». После отклонения появляется кнопка «🔄 Сбросить статус» — она **удаляет** пользователя из `users.yaml`, чтобы он мог подать заявку заново (`reject_user` этого не делает).
4. **RBAC (Role-Based Access Control):** В `config/roles.yaml` описываются роли и права на таблицы/поля. `RBACGuard` физически блокирует запросы к запрещённым таблицам и полям. Проверка идёт по полному имени `schema.table`; `SELECT *` запрещён, если у таблицы есть `excluded_columns` **или** явный белый список `allowed_columns` (защита от утечки полей вне списка). При отказе именно из-за `SELECT *` ядро пробует развернуть звёздочку в разрешённые поля (`expand_select_star`) и повторить проверку — безопасность при этом не ослабляется (подставляются только разрешённые поля; технические поля отбрасываются). Правила могут задаваться как для конкретных таблиц, так и в виде `schema.*` — `RBACGuard` разворачивает такие шаблоны в разрешённые таблицы с учётом метаданных БД.
   **Порядок проверки:** точное `schema.table` → `*` (все таблицы) → `schema.*` (все таблицы схемы) → короткое имя **только если схема в SQL не указана**. Правило без схемы не даёт доступа к одноимённой таблице в другой схеме.
   **COUNT(\*)** не блокируется правилом `excluded_columns`/`allowed_columns`: агрегат не возвращает поля, поэтому `SELECT COUNT(*) FROM t` разрешён даже если у `t` есть скрытые поля.
   **Неквалифицированные колонки при нескольких таблицах:** требуем, чтобы колонка была разрешена **во ВСЕХ** таблицах запроса (или имела wildcard). Это закрывает `SELECT secret_col FROM t1 JOIN t2`, где `t1` разрешает `secret_col`, а `t2` — нет.
5. **Защита от P2SQL:** `RBACGuard` блокирует доступ к системным таблицам (`information_schema`, `pg_catalog`, `sqlite_master`) в любом месте AST (включая подзапросы и UNION) — **до** применения RBAC-правил, wildcard `*` их не открывает. `validator.py` работает в режиме fail-closed: любая ошибка парсинга `sqlglot` — отказ.
6. **Защита админов:** `add_user` игнорирует заявки от env-админов и от пользователей с ролью `admin` в YAML. `delete_user` не удаляет env-админов. `set_user_roles`, `toggle_user_active` не трогают env-админов. `/users` (админ-панель) доступна **только env-админам** (не просто `role: admin` в YAML).

## 🛠️ 7. Служебные команды

| Команда | Описание |
|---|---|
| `/start` | Запуск бота / регистрация / приветствие. |
| `/stop` | **Прервать текущий анализ** (отменяет фоновую задачу). Работает только для задач, запущенных в текущем процессе (см. раздел 9). В CLI сам `/stop` уходит в LLM — используйте `Ctrl+C`. |
| `/help` | Справка (читается из `config/help.md`). |
| `/db`, `/db <name>` | Текущая БД / выбор новой. При выборе выводится схема по умолчанию. |
| `/model`, `/model <name>` | Текущая модель / переключение (регистронезависимо). Разрешает сокращённое имя (точное совпадение без учёта регистра или уникальный префикс `<short>_`). |
| `/models` | Список доступных моделей из `models.yaml`. |
| `/sql <query>` | Выполнить SQL вручную (только для одобренных, проходит `validator` + RBAC). **Ограничение сложности (`check_complexity`) к `/sql` не применяется** (осознанное решение владельца). Каждый вызов пишется в `logs/manual_sql.log` (полный SQL, `user_id`, `db_name`, длительность, результат) через `app/core/manual_sql_audit.py`. Технические таблицы/колонки блокируются и для админов. При отказе по `SELECT *` — авторазвёртывание (`expand_select_star`). |
| `/history` | Показать историю диалога (первые 300 символов каждого сообщения). |
| `/compact` | Сжать историю (LLM-резюме; при недоступности LLM — сообщение об ошибке, история не теряется). |
| `/clear` | Полностью очистить историю (память сохраняется). |
| `/reset` | Полный сброс: история + память + `last_sqls`. |
| `/remember <текст>` | Запомнить факт в долгосрочную память. |
| `/memory` | Показать память (с кнопками управления). |
| `/forget <номер>` | Удалить факт по номеру (текстовая команда). |
| `/tokens` | Показать расход токенов контекста (оценка: `chars // 4`). |
| `/settings` | Текущие настройки (БД, модель, лимиты, доступные БД/модели/схемы, схема по умолчанию, таймаут SQL). |
| `/users` | **Только env-админы.** Админ-панель: список пользователей с пагинацией, карточка, изменение ролей, блокировка/разблокировка, удаление. |

**CLI-адаптер** дополнительно поддерживает локальные команды: `:help`, `:clear`, `:quit` (или `exit`). `Ctrl+C` отменяет текущий запрос (аналог `/stop`; сам `/stop` в CLI уходит в LLM). При старте CLI предупреждает, если `CLI_USER_ID` не админ (RBAC заблокирует таблицы).

## ⚙️ 8. Инструкция по настройке

### 8.1. Локальная разработка
1. Установите Python 3.12 и Pipenv.
2. `pipenv install --dev`.
3. Создайте `.env` и `config/*.yaml` (см. примеры ниже).
4. Запустите Redis (опционально — бот работает и без него, но история/память/сессия не кэшируются).
5. Запустите `./prj_start.sh` (или `python -m app.main`).

При старте `.env` загружается до выбора адаптера (`load_dotenv(override=False)`); выбранный адаптер (`telegram`/`cli`) и причина выбора пишутся в лог.

**⚠️ Важно про CLI и `pipenv run`:** `pipenv run` загружает `.env` раньше, чем `main()`, и **перезаписывает** env-переменные из командной строки. Поэтому `ADAPTER=cli pipenv run python -m app.main` запустит **Telegram** (если в `.env` `ADAPTER=telegram`), а не CLI. Диагностика в `_build_adapter` предупредит об этом WARNING'ом.

**Правильный запуск CLI (без Telegram):**

```bash
ADAPTER=cli CLI_USER_ID=<admin_id> .venv/bin/python -m app.main
```

(прямой вызов интерпретатора из venv, без `pipenv run` — `.env` не загружается автоматически, `ADAPTER=cli` из командной строки сохраняется).

**Альтернатива** — `PIPENV_DONT_LOAD_ENV=1`:

```bash
PIPENV_DONT_LOAD_ENV=1 ADAPTER=cli CLI_USER_ID=<admin_id> pipenv run python -m app.main
```

### 8.2. Файл `.env` (пример)
```env
# Telegram
TELEGRAM_TOKEN=ваш_токен
ADAPTER=telegram            # или cli

# Администраторы (ID через запятую). Им не нужна регистрация.
ADMIN_USERS=123456789,987654321

# CLI (для отладки без Telegram)
CLI_USER_ID=412719813       # ID админа из ADMIN_USERS — иначе RBAC заблокирует всё

# LLM (DeepSeek через OpenCode Zen)
LLM_TOOL_URL=https://opencode.ai/inference/openai/v1
LLM_TOOL_TOKEN=sk-ваш_ключ
LLM_TOOL_MODEL=deepseek-v4.1-flash
DEFAULT_MODEL=tool_calling_model

# Базы данных
DB_MAIN_HOST=...
DB_MAIN_USER=...
DB_MAIN_PASSWORD=...
DEFAULT_DB=db_itzamna       # опционально: БД по умолчанию

# Redis (опционально)
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=

# Лимиты
SQL_QUERY_TIMEOUT=15
LOCK_WAIT_TIMEOUT=5             # ожидание блокировки (MySQL/MariaDB), сек
MAX_ROWS_IN_RESPONSE=100
MAX_MESSAGE_LENGTH=4000
SESSION_TTL_SEC=86400       # 24 часа
AGENT_MAX_STEPS=10          # лимит шагов Agent Loop (минимум 1)
SHOW_MODEL_IN_RESPONSE=false
SCHEMA_CACHE_TTL_SEC=3600       # TTL кэша схемы БД (сек)

# Проверка сложности SQL (check_complexity)
SQL_MAX_JOINS=5
SQL_MAX_SUBQUERY_DEPTH=3
SQL_MAX_UNIONS=3

# Скрытые (технические) таблицы/колонки
TECHNICAL_PREFIXES=technical_       # CSV-префиксы скрытых таблиц/колонок

# Контекст LLM
DEFAULT_CONTEXT_LENGTH=32768

# Логи (ротация)
LOG_MAX_BYTES=104857600         # 100 MB (audit.log, manual_sql.log)
LOG_BACKUP_COUNT=5
SECURITY_LOG_MAX_BYTES=52428800 # 50 MB (security.log)
SECURITY_LOG_BACKUP_COUNT=5
```

### 8.3. `config/models.yaml` (пример)
```yaml
models:
  tool_calling_model:
    model_name: deepseek-v4.1-flash
    base_url: ${LLM_TOOL_URL}
    api_key: ${LLM_TOOL_TOKEN}
    supports_tools: true
    temperature: 0.0
    max_tokens: 1500         # лимит длины ответа (экономия токенов)
    context_length: 32768
```

### 8.4. `config/databases.yaml` (пример)
```yaml
databases:
  db_itzamna:
    type: mysql
    host: ${DB_MAIN_HOST}
    port: 3306
    user: ${DB_MAIN_USER}
    password: ${DB_MAIN_PASSWORD}
    database: db_itzamna
    schemas: auto           # или явный список: [db_itzamna, db_itzamna_archive]
    exclude_schemas: [mysql, information_schema, performance_schema, sys]
    description: "Основная БД"
    readonly: true
    is_mariadb: false       # true — MariaDB: вместо MAX_EXECUTION_TIME
                            # бот выставит max_statement_time (в секундах)
    charset: utf8mb4
    minsize: 1
    maxsize: 5
    # Доп. SQL-команды на каждое соединение пула (readonly и таймаут
    # бот ставит сам). Поддерживается подстановка {timeout} = SQL_QUERY_TIMEOUT.
    # Ограничение: не используйте ';' внутри строковых литералов —
    # CLIENT.MULTI_STATEMENTS разорвёт команду (бот напишет WARNING).
    # Сам CLIENT.MULTI_STATEMENTS включается только если итоговый init_cmd
    # содержит ';' (то есть команд действительно несколько).
    init_statements: []
    # Пример: ["SET SESSION sql_mode = 'STRICT_TRANS_TABLES'"]
```

**Единые ключи размера пула:** `minsize`/`maxsize` используются и для MySQL, и для PostgreSQL (унификация). Для PostgreSQL они передаются в `asyncpg.create_pool` как `min_size`/`max_size`. Старые ключи `min_size`/`max_size` в конфиге PostgreSQL-БД больше **не читаются** — переименуйте их в `minsize`/`maxsize` (иначе будут взяты дефолты 1/5).

### 8.5. `config/help.md` — обновлённая справка
`/help` читает файл `config/help.md`. Убедитесь, что в нём упомянуты все команды из раздела 7 (особенно `/stop`, `/models`, `/settings`, `/sql`, `/remember`, `/memory`, `/users` для env-админов).

## ⚠️ 9. Ограничения и известные проблемы

### 9.1. Принятые архитектурные риски
- **Одновременные запросы:** `asyncio.Lock` на каждого пользователя. Разные пользователи работают параллельно, один — последовательно.
- **Сессии при рестарте (P1, принятый риск):** `SessionState` целиком в Redis **не** сохраняется. В `session:{user_id}` зеркалятся только `current_db`, `current_model`, `summary`, `last_sqls`; история диалога живёт отдельно (`repdix:history:{user_id}`), память — в `repdix:memory:{user_id}`. Объекты `asyncio.Lock` и `current_task` между процессами не переносимы, поэтому после рестарта незавершённый запрос не «докручивается», а `/stop` не может отменить задачу, запущенную до рестарта. Полная сериализация сессии — отдельная задача (`TODO(P1)` в `app/core/session.py`); до её решения потеря этого куска контекста при рестарте считается приемлемой.
- **`/sql` без `check_complexity`:** осознанное решение владельца. Внутренние сотрудники могут случайно запустить тяжёлый запрос (например, `SELECT * FROM huge JOIN huge2`); серверный `MAX_EXECUTION_TIME` прервёт его через `SQL_QUERY_TIMEOUT`, но 15 секунд × N сотрудников = деградация. Рекомендация: добавить `EXPLAIN` + предупреждение или принудительный `LIMIT` (в текущей версии **не реализовано**).
- **`databases.yaml` не hot-reload:** пулы соединений создаются один раз при старте. Для смены хоста, пароля, `readonly` или `init_statements` нужен перезапуск бота. Остальные конфиги (`models.yaml`, `roles.yaml`, `users.yaml`, `relations.yaml`, `templates.yaml`, `help.md`) перечитываются по mtime.

### 9.2. Известные ограничения (не баги)
- **`/sql` без `check_complexity`:** осознанное решение владельца.
  Внутренние сотрудники могут случайно запустить тяжёлый запрос;
  серверный `MAX_EXECUTION_TIME` прервёт его через `SQL_QUERY_TIMEOUT`.
  Рекомендация: `EXPLAIN` + предупреждение (не реализовано).
- **`asyncio.wait_for` в MySQL не отменяет серверный запрос:**
  `aiomysql` не шлёт `KILL QUERY`. Основная защита — `MAX_EXECUTION_TIME`
  (ставится в `init_command`, включая `readonly=true`). Задокументировано
  в `db_manager.execute_query`.
- **`databases.yaml` без hot-reload:** пулы создаются один раз при старте.
  Смена хоста/пароля/`readonly` требует перезапуска.
- **P1: сессии при рестарте:** `asyncio.Lock`/`current_task` не
  сериализуются в Redis. После рестарта незавершённый запрос не
  «докручивается», `/stop` не отменяет старые задачи. Принятый риск.
- **`SHOW GRANTS` при старте не проверяется:** ответственность админа —
  подключить бота под пользователем БД с правами только `SELECT`.
- **`_format_sql_result` дублирует имя колонки для `SELECT 'literal'`:**
  `SELECT 'SET GLOBAL x'` вернёт `1. 'SET GLOBAL x': SET GLOBAL x` (UX, не
  безопасность). Отдельная задача.
- **CLI: `/stop` уходит в LLM:** в отличие от Telegram, `cli_adapter` не
  обрабатывает `/stop` — используйте `Ctrl+C`. Отдельная задача.
- **`MULTI_STATEMENTS` включается только при `;` в `init_cmd`:**
  если итоговый `init_cmd` — один `SET SESSION` без `;`, флаг
  `CLIENT.MULTI_STATEMENTS` не выставляется (`client_flag=0`), чтобы не
  расширять поверхность атаки. При нескольких `SET SESSION` (склейка через
  `; `) флаг выставляется — иначе они не выполнятся. Это осознанное
  сужение, а не баг.

### 9.3. Ответственность админа (не баги кода)
- **Права БД только `SELECT`:** `readonly: true` не заменяет права БД. При старте проверка `SHOW GRANTS` **не реализована** — ответственность на админе.
- **`init_statements`:** не используйте `;` внутри строковых литералов — `CLIENT.MULTI_STATEMENTS` разорвёт команду. Бот пишет WARNING, если `;` вне литералов.
- **`slow_query_log`:** диагностика, не защита. Включается на стороне DBA.
- **Redis без пароля на не-loopback хосте:** бот **упадёт при старте** с `ValueError` (CVE-2025-21605). Разрешён только loopback без пароля.
- **Файлы конфигурации:** `config/users.yaml` содержит персональные данные (ФИО). **Обязательно** добавьте `config/users.yaml`, `.env` и `logs/` в `.gitignore`, чтобы не закоммитить их в публичный репозиторий.
- **CLI-запуск через `pipenv run` + `ADAPTER=cli`:** `pipenv` перезапишет `ADAPTER` из `.env` на `telegram`. Используйте прямой запуск `.venv/bin/python -m app.main` или `PIPENV_DONT_LOAD_ENV=1`.

## 🧪 10. Тестирование
Проект использует `pytest` + `pytest-asyncio`. **Фактическое покрытие: 16 файлов, 183 теста** (`pytest --collect-only -q tests/`). Ключевые тесты:

- `tests/test_validator.py` — `validate_sql`: разрешённые/запрещённые запросы, комментарии, multiple statements, null bytes, fail-closed.
- `tests/test_validator_comments.py` — комментарии SQLite без пробела после `--`, строковые литералы, multiple statements.
- `tests/test_validator_literals.py` — `DANGEROUS_PATTERNS` только вне литералов: `SELECT 'SET GLOBAL x'` разрешён, `SELECT SLEEP(5)` — нет.
- `tests/test_rbac_guard.py` — `RBACGuard.validate_sql`: qualified-таблицы, `SELECT *` + excluded/allowed, обход через смену схемы, системные таблицы, P2SQL.
- `tests/test_rbac_guard_multi_schema.py` — разворачивание правил `schema.*`.
- `tests/test_rbac_guard_ambiguous_column.py` — неквалифицированные колонки: `all` (не `any`) при нескольких таблицах.
- `tests/test_rbac_guard_count_star.py` — `COUNT(*)` не блокируется `excluded_columns`/`allowed_columns`.
- `tests/test_rbac_select_star.py` — `expand_select_star` и его интеграция в engine.
- `tests/test_engine_extract_table.py` — `_extract_table_from_sql` и `_extract_last_table_from_history` через `sqlglot`: кавычки, CTE, подзапросы; системные таблицы пропускаются.
- `tests/test_engine_rbac_msg_not_sql.py` — защита в глубину: не-SQL `rbac_msg` (пустая строка, текст ошибки, пробелы) не подставляется в `execute_query`.
- `tests/test_db_manager_pg_pool_sizes.py` — PostgreSQL-пул читает `minsize`/`maxsize` (дефолты 1/5).
- `tests/test_engine_select_star_violation.py` — неудача `expand_select_star` пишется в `audit.log`, не в `security.log`.
- `tests/test_engine_agent_steps.py` — `AGENT_MAX_STEPS` (default, zero, negative, non-numeric, custom).
- `tests/test_db_manager_mysql_timeout.py` — `init_command` (readonly + таймаут, MariaDB `max_statement_time`, `init_statements`, warning про `;`, `client_flag` MULTI_STATEMENTS только при `;`).
- `tests/test_db_manager_schemas.py` — `get_schemas` (explicit, fallback, auto, **auto failure → `[]`**), `get_table_list`, кэш.
- `tests/test_llm_client.py` — `get_client` кэширование, hot-reload, `_orphaned_clients` закрываются в `close()`.
- `tests/test_main_env_int.py` — `_env_int`: invalid → default + WARNING, zero/negative → minimum, empty → default.
- `tests/test_manual_sql_audit.py` — `log_manual_sql` (успех, ошибка), `/sql` пишется в `manual_sql.log`.
- `tests/test_session_cleanup.py` — `start_cleanup_task`/`stop_cleanup_task`, идемпотентность, удаление просроченных.
- `tests/test_sql_complexity.py` — JOIN, декартово произведение, глубина подзапросов, UNION, fail-closed.
- `tests/test_technical_filter.py` — `is_technical`, `is_technical_qualified`, env-префиксы.
- `tests/test_telegram_long_message.py` — `_split_long_text`: `\n`, пробел, жёсткая нарезка, **не внутри HTML-тега**.
- `tests/test_telegram_sanitize.py` — `sanitize_telegram_html`: `<script>` экранируется, безопасные теги сохраняются, `javascript:` отсекается.
- `tests/test_user_manager.py` — `_save_users_yaml` вызывает `fsync`, `OSError` не ломает запись.
- `tests/test_format.py` — форматирование результата SQL (пусто, одна строка, много колонок, обрезка).
- `tests/test_engine_format_caches_text_columns.py` — кэш `text_columns` в `_format_sql_result` (один вызов `get_table_schema` на таблицу).

Запуск: `pytest -v`.

**Регрессионные тесты для закрытых багов** (добавлены в этой сессии):
- `test_rbac_guard_ambiguous_column.py` — `SELECT secret_col FROM t1 JOIN t2` (после `any` → `all`).
- `test_validator_literals.py` — `SELECT 'SET GLOBAL x'` (после вырезания литералов).
- `test_engine_extract_table.py` — `` `db`.`t` ``, `WITH x AS (...) SELECT ... FROM x` (после перехода на `sqlglot`); `_extract_last_table_from_history` тоже на `sqlglot` (кавычки, CTE, подзапросы).
- `test_engine_rbac_msg_not_sql.py` — `rbac_msg` не подставляется в `sql`, если это не SQL (пустая строка/текст ошибки/пробелы).
- `test_engine_format_caches_text_columns.py` — кэш `text_columns` в `_format_sql_result` (один `get_table_schema` на таблицу).
- `test_db_manager_pg_pool_sizes.py` — `minsize`/`maxsize` для PostgreSQL-пула (унификация с MySQL).
- `test_db_manager_mysql_timeout.py::test_client_flag_multi_when_semicolon` — `client_flag == CLIENT.MULTI_STATEMENTS`, когда `init_cmd` содержит `;` (несколько `SET SESSION`).
- `test_db_manager_mysql_timeout.py::test_client_flag_zero_when_single_statement` — `client_flag == 0`, когда `init_cmd` — один `SET SESSION` без `;`.
- `test_rbac_guard_count_star.py` — `COUNT(*)` при `excluded_columns`.
- `test_engine_select_star_violation.py` — `expand_select_star_failed` в `audit.log`, не в `security.log`.
- `test_user_manager.py` — `fsync` в `_save_users_yaml`.
- `test_db_manager_mysql_timeout.py::test_init_statements_warns_on_semicolon` — warning про `;` в `init_statements`.
- `test_session_cleanup.py` — фоновая очистка TTL.
- `test_db_manager_schemas.py::test_get_schemas_auto_failure_returns_empty` — `get_schemas(auto)` → `[]`.
- `test_llm_client.py::test_orphaned_clients_on_no_loop` — `_orphaned_clients` закрываются.
- `test_main_env_int.py` — безопасный парсинг env.
- `test_telegram_long_message.py` — нарезка не внутри HTML-тегов.

## 📋 11. Лицензия и контакты
Проект распространяется под лицензией **MIT**. Полный текст — в файле [`LICENSE.md`](LICENSE.md).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

- **Автор:** Дмитрий Неупокоев
- **GitHub:** [github.com/dneupokoev](https://github.com/dneupokoev)
- **Репозиторий проекта:** [github.com/dneupokoev/repdix](https://github.com/dneupokoev/repdix)