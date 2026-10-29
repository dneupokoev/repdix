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
- **Парсер SQL:** `sqlglot` (диалект mysql для RBAC-разбора).

## 🏗️ 3. Архитектура (Порты и Адаптеры)
Ядро ничего не знает о Telegram. Общение идёт через абстрактные интерфейсы.

```text
repdix/
├── app/
│   ├── __init__.py
│   ├── main.py                 # точка входа: логирование, инициализация сервисов, выбор адаптера, graceful shutdown
│   ├── core/                   # бизнес-логика (независима от мессенджера)
│   │   ├── __init__.py
│   │   ├── engine.py           # ядро: Agent Loop, tool_calls, RBAC, формирование промпта (включая правило обрамления идентификаторов MySQL)
│   │   ├── session.py          # SessionState (история, lock, current_task, last_sqls, summary, long_term_facts) + SessionStore с TTL
│   │   ├── db_manager.py       # пулы соединений (MySQL/PostgreSQL/SQLite), таймауты, read-only, метаданные, multi-schema
│   │   ├── user_manager.py     # управление доступом (hot-reload users.yaml), add/approve/reject/delete
│   │   ├── llm_client.py       # клиент LLM (hot-reload models.yaml, temperature, max_tokens, fallback tools)
│   │   ├── cache_manager.py    # Redis (история диалогов, graceful degradation)
│   │   ├── rbac_guard.py       # проверка прав доступа к таблицам и полям (sqlglot AST), правила schema.*, защита от P2SQL
│   │   ├── technical_filter.py # фильтрация технических таблиц/колонок (TECHNICAL_PREFIXES)
│   │   └── validator.py        # SQL-валидатор (только SELECT/WITH, forbidden keywords, fail-closed)
│   └── interfaces/             # адаптеры мессенджеров
│       ├── __init__.py
│       ├── base.py             # ChatMessage (с колбэком статуса), ChatResponse (с reply_markup), BaseChatInterface
│       ├── telegram_adapter.py # Telegram (aiogram), FSM-регистрация, HTML-разметка, кнопки управления памятью, /stop, модерация
│       └── cli_adapter.py      # Консольный интерфейс (отладка, CLI_USER_ID)
├── config/
│   ├── databases.yaml          # шаблоны подключений к целевым БД (${VAR} из .env)
│   ├── models.yaml             # шаблоны моделей LLM (temperature, context_length, max_tokens, supports_tools)
│   ├── relations.yaml          # описание связей (для JOIN)
│   ├── templates.yaml          # детерминированные шаблоны (топы/рейтинги, только MySQL)
│   ├── users.yaml              # список пользователей, ролей и статусов (НЕ КОММИТИТЬ)
│   ├── roles.yaml              # описание ролей и прав (RBAC)
│   └── help.md                 # справка /help
├── data/                       # локальные данные (опционально)
├── tests/                      # pytest
├── logs/                       # audit.log, security.log (ротация, НЕ КОММИТИТЬ)
├── .env                        # секреты (НЕ КОММИТИТЬ)
└── .gitignore
```

### 3.1. Ключевые модули
- `app/main.py` — запуск приложения, инициализация сервисов, выбор адаптера (`telegram` или `cli`), корректное завершение.
- `app/core/engine.py` — Agent Loop, вызовы инструментов, проверки RBAC, сборка системного промпта, динамическая схема БД, авто-сжатие истории, управление памятью.
- `app/core/session.py` — состояние сессии, история, блокировки, активные задачи, резюме диалога (`summary`), долгосрочная память (`long_term_facts`) и TTL-хранилище сессий.
- `app/core/db_manager.py` — пулы подключений к MySQL/PostgreSQL/SQLite, таймауты, read-only-режим, кэш метаданных и поддержка нескольких схем (`multi-schema`).
- `app/core/llm_client.py` — клиент LLM, hot-reload `models.yaml`, `max_tokens`, `temperature`, fallback-инструменты.
- `app/core/cache_manager.py` — Redis-кэш истории диалогов, graceful degradation при недоступности Redis.
- `app/core/rbac_guard.py` — контроль доступа к таблицам и полям на основе AST `sqlglot`, правила вида `schema.*`, блокировка системных таблиц и `SELECT *` при наличии ограничений.
- `app/core/technical_filter.py` — скрытие технических таблиц и колонок по префиксам из `TECHNICAL_PREFIXES`.
- `app/core/validator.py` — проверка SQL: только `SELECT`/`WITH`, запрещённые ключевые слова, fail-closed парсинг через `sqlglot`.
- `app/core/user_manager.py` — модерация пользователей, hot-reload `users.yaml`, защита администраторов.
- `app/interfaces/base.py` — абстрактные интерфейсы сообщений (`ChatMessage`) и ответов (`ChatResponse`, включая `reply_markup`).
- `app/interfaces/telegram_adapter.py` — Telegram-адаптер: FSM-регистрация, HTML-разметка ответов (`parse_mode`), санитайзер HTML, кнопки управления памятью, `/stop`, модерация.
- `app/interfaces/cli_adapter.py` — консольный адаптер для отладки.

### 3.2. Поток обработки запроса
1. Адаптер (`telegram_adapter.py` или `cli_adapter.py`) принимает сообщение пользователя и передаёт его в ядро.
2. `engine.py` формирует системный промпт с учётом динамической схемы БД, истории, долгосрочной памяти и RBAC-правил.
3. LLM вызывает инструменты (`execute_sql`, `get_relations`), ядро валидирует SQL через `validator.py`, проверяет доступ через `rbac_guard.py` и скрывает технические объекты через `technical_filter.py`.
4. `db_manager.py` выполняет запрос в целевой БД с учётом таймаутов и read-only-режима.
5. Статусы Chain of Thought отправляются через `status_callback`; ответ пользователю рендерится адаптером (в Telegram — с `parse_mode=HTML`).

## 🗄️ 4. Работа с базами данных
- **Шаблоны в `config/databases.yaml`**: реальные значения подставляются из `.env` через `${VAR}`.
- **Ограничения на стороне СУБД.** Бот должен работать под пользователем БД, у которого есть **только права `SELECT`**. `readonly: true` и валидатор SQL — дополнительные барьеры, но **не замена** правам на уровне БД (см. раздел 4.1).
- **Таймауты:** `SQL_QUERY_TIMEOUT` (по умолчанию 15 секунд) прерывает тяжёлые запросы:
  - **MySQL:** `SET SESSION MAX_EXECUTION_TIME` (только MySQL 5.7.8+, не MariaDB). Ставится через `init_command` при создании пула.
  - **PostgreSQL:** `statement_timeout` в `server_settings` (прерывает запрос на стороне сервера).
  - **SQLite:** `asyncio.wait_for` вокруг `cursor.fetchall()`.
- **Режим транзакций read-only (выставляется при `readonly: true`):**
  - **MySQL:** `SET SESSION transaction_read_only = 1` (при `readonly: true`, не MariaDB). ⚠️ В режиме `readonly: true` серверный таймаут (`MAX_EXECUTION_TIME`) не ставится. Настоятельно рекомендуется использовать пользователя БД только с правами `SELECT` и таймаутом на уровне сервера. В коде добавлен `asyncio.wait_for` как последняя линия защиты.
  - **PostgreSQL:** `default_transaction_read_only = on` в `server_settings`.
  - **SQLite:** `PRAGMA query_only = ON`.
- **MariaDB:** установите `is_mariadb: true` в `databases.yaml`, чтобы отключить `MAX_EXECUTION_TIME` (MariaDB его не понимает).
- **Multi-schema:** одна MySQL-БД может содержать несколько схем. Список задаётся в `databases.yaml` (`schemas: auto` или явный список), исключения — `exclude_schemas`. Результат кэшируется на `SCHEMA_CACHE_TTL_SEC` (по умолчанию 3600 сек). Таблицы в промпте и в `get_table_list()` возвращаются fully-qualified (`schema.table`); `get_table_schema()` принимает `(db, table, schema)`.

### 4.1. 🔐 Безопасность базы данных

⚠️ **ВАЖНО:** Для безопасности бот должен подключаться к базе данных под пользователем, у которого есть **ТОЛЬКО права на `SELECT` (чтение)**. Параметр `readonly: true` в `databases.yaml` **не заменяет** права БД, он лишь выставляет режим транзакций (`SET SESSION transaction_read_only = 1`, `default_transaction_read_only = on`, `PRAGMA query_only = ON`). Если у пользователя БД есть права на запись, скомпрометированная LLM может изменить данные.

Валидатор SQL (`validate_sql`) и RBAC-гард — лишь дополнительный барьер; они не заменяют ограничения прав на стороне СУБД и не гарантируют неизменность данных.

## 🧠 5. Работа с моделями и AI-Агент

### 5.1. Agent Loop (Цикл агента)
В `engine.py` реализован цикл `while step < max_steps`. Лимит шагов задаётся переменной окружения `AGENT_MAX_STEPS` (по умолчанию 10, минимум 1): пяти шагов не хватало сложным вопросам (несколько SQL подряд + финальный вывод). Модель может: изучить структуру таблиц (из системного промпта), выполнить несколько SQL-запросов, сравнить результаты и написать бизнес-вывод. Если цикл оборвался по лимиту и модель так и не вернула текст, в лог пишется `WARNING "Agent loop reached max_steps"` с полями `steps`, `max_steps`, `user_id`.
**Важно:** В цикле сообщения модели обязательно передаются с ролью `"tool"` и `tool_call_id`, иначе DeepSeek вернёт ошибку 400.

В системный промпт добавлено правило обрамления идентификаторов MySQL обратными кавычками. При формировании простых ответов имена таблиц скрываются от пользователя. Для экономии токенов в `config/models.yaml` можно задать `max_tokens` (лимит длины ответа).

### 5.2. Chain of Thought (Прозрачность)
Перед каждым вызовом инструмента модель пишет краткое пояснение.
**Как это работает:** В `ChatMessage` есть `status_callback`. `telegram_adapter.py` отправляет эти статусы пользователю (вида `🤖 Сейчас посмотрю структуру...`). В `engine.py` текст перехватывается и отправляется через колбэк. Статусы автоматически обрезаются до 80 символов с учётом HTML-разметки, чтобы не засорять чат и не ломать теги. `cli_adapter.py` печатает статусы с отступом. Ответы Telegram отправляются с `parse_mode=HTML`.

### 5.3. Инструменты модели (Tool Calling)
Доступны инструменты:
- `execute_sql` — для сложных аналитических SELECT-запросов.
- `get_relations` — узнать связи таблиц.
- **⚠️ Важно:** `SHOW COLUMNS` и `SHOW TABLES` **не пройдут** `validator.validate_sql` — только `SELECT` и `WITH`. Вся нужная информация о структуре уже в промпте (динамическая схема).

### 5.4. Управление историей, контекстом и долгосрочной памятью
- **Динамическая схема БД:** в промпт передаются поля только для тех таблиц, которые упомянуты в вопросе, последних 4 сообщениях истории или последних SQL. Если совпадений нет — передаётся только список имён таблиц (экономия контекста).
- **Auto-Compact:** при достижении 60% лимита контекста первые 4 сообщения истории сжимаются LLM в 2 предложения и заменяются одним `system`-сообщением (резюме сохраняется в `state.summary` и в Redis).
- **Долгосрочная память:** пользователь может сохранять факты (`/remember`, кнопка «Добавить») в Redis (TTL 30 дней). Факты добавляются в системный промпт, и LLM обязана учитывать их как постоянные фильтры (например, «работаю только с центром X»).
- **Управление памятью:** команда `/memory` выводит список фактов с Inline-кнопками (Изменить, Удалить, Добавить, Очистить всё). Callback'и привязаны к хэшу текста факта (а не к индексу), чтобы избежать смещения при удалении.
- **`/clear`** — очищает только историю диалога (долгосрочная память сохраняется).
- **`/reset`** — полный сброс: история, память, `last_sqls`.
- **`/compact`** — принудительное сжатие истории.
- **`/stop`** — прерывание текущего анализа (отмена `asyncio.Task`). При рестарте бота незавершённые задачи не «докручиваются» (объекты `asyncio.Lock` и `current_task` не сериализуются).

## 🛡️ 6. Управление доступом (Модерация и RBAC)
Доступ к боту строго контролируется:
1. **Администраторы** (указаны в `ADMIN_USERS` в `.env` или имеют роль `admin` в `users.yaml`) имеют доступ ко всему. При старте `user_manager.sync_admins()` гарантирует, что все ID из `.env` имеют `is_active: true` и `roles: ["admin"]`.
2. **Обычные пользователи** при первом `/start` проходят интервью (ФИО, Отдел, Цель). Данные сохраняются в `config/users.yaml` (с флагом `is_active: false`). Пока админ не одобрит заявку, бот не пустит пользователя к LLM. **Файл перечитывается «на лету» (hot-reload по mtime) при каждом запросе, перезапуск не нужен.**
3. **Модерация:** админ получает уведомление с кнопками «✅ Одобрить» / «❌ Отклонить». После отклонения появляется кнопка «🔄 Сбросить статус» — она **удаляет** пользователя из `users.yaml`, чтобы он мог подать заявку заново (`reject_user` этого не делает).
4. **RBAC (Role-Based Access Control):** В `config/roles.yaml` описываются роли и права на таблицы/поля. `RBACGuard` физически блокирует запросы к запрещённым таблицам и полям. Проверка идёт по полному имени `schema.table`; `SELECT *` запрещён, если у таблицы есть `excluded_columns` **или** явный белый список `allowed_columns` (защита от утечки полей вне списка). Правила могут задаваться как для конкретных таблиц, так и в виде `schema.*` — `RBACGuard` разворачивает такие шаблоны в разрешённые таблицы с учётом метаданных БД.
5. **Защита от P2SQL:** `RBACGuard` блокирует доступ к системным таблицам (`information_schema`, `pg_catalog`, `sqlite_master`) в любом месте AST (включая подзапросы и UNION). `validator.py` работает в режиме fail-closed: любая ошибка парсинга `sqlglot` — отказ.
6. **Защита админов:** `add_user` игнорирует заявки от env-админов и от пользователей с ролью `admin` в YAML. `delete_user` не удаляет env-админов.

## 🛠️ 7. Служебные команды

| Команда | Описание |
|---|---|
| `/start` | Запуск бота / регистрация / приветствие. |
| `/stop` | **Прервать текущий анализ** (отменяет фоновую задачу). |
| `/help` | Справка (читается из `config/help.md`). |
| `/db`, `/db <name>` | Текущая БД / выбор новой. |
| `/model`, `/model <name>` | Текущая модель / переключение (регистронезависимо). |
| `/models` | Список доступных моделей. |
| `/sql <query>` | Выполнить SQL вручную (только для одобренных, проходит validator + RBAC). |
| `/history` | Показать историю диалога. |
| `/compact` | Сжать историю (LLM-резюме). |
| `/clear` | Полностью очистить историю (память сохраняется). |
| `/reset` | Полный сброс: история + память + `last_sqls`. |
| `/remember <текст>` | Запомнить факт в долгосрочную память. |
| `/memory` | Показать память (с кнопками управления). |
| `/forget <номер>` | Удалить факт по номеру (текстовая команда). |
| `/tokens` | Показать расход токенов контекста (оценка). |
| `/settings` | Текущие настройки (БД, модель, лимиты, доступные БД/модели, таймаут SQL). |

**CLI-адаптер** дополнительно поддерживает локальные команды: `:help`, `:clear`, `:quit` (или `exit`). Ctrl+C отменяет текущий запрос (аналог `/stop`).

## ⚙️ 8. Инструкция по настройке

### 8.1. Локальная разработка
1. Установите Python 3.12 и Pipenv.
2. `pipenv install --dev`.
3. Создайте `.env` и `config/*.yaml` (см. примеры ниже).
4. Запустите Redis (опционально — бот работает и без него, но история не кэшируется).
5. Запустите `./prj_start.sh` (или `python -m app.main`).

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
MAX_ROWS_IN_RESPONSE=100
MAX_MESSAGE_LENGTH=4000
SESSION_TTL_SEC=86400       # 24 часа
AGENT_MAX_STEPS=10          # лимит шагов Agent Loop (минимум 1)
SHOW_MODEL_IN_RESPONSE=false
SCHEMA_CACHE_TTL_SEC=3600       # TTL кэша схемы БД (сек)

# Скрытые (технические) таблицы/колонки
TECHNICAL_PREFIXES=technical_       # CSV-префиксы скрытых таблиц/колонок

# Контекст LLM
DEFAULT_CONTEXT_LENGTH=32768

# Логи (ротация)
LOG_MAX_BYTES=104857600         # 100 MB (audit.log)
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
    is_mariadb: false       # true, если СУБД — MariaDB (отключает MAX_EXECUTION_TIME)
    charset: utf8mb4
    minsize: 1
    maxsize: 5
```

### 8.5. `config/help.md` — обновлённая справка
`/help` читает файл `config/help.md`. Убедитесь, что в нём упомянуты все команды из раздела 7 (особенно `/stop`, `/models`, `/settings`, `/sql`, `/remember`, `/memory`).

## ⚠️ 9. Ограничения и известные проблемы
- **Одновременные запросы:** `asyncio.Lock` на каждого пользователя. Разные пользователи работают параллельно, один — последовательно.
- **Сессии при рестарте (P1, принятый риск):** `SessionState` целиком в Redis **не** сохраняется. В `session:{user_id}` зеркалятся только `current_db`, `current_model`, `summary`, `last_sqls`, `long_term_facts`; история диалога живёт отдельно (`repdix:history:{user_id}`), память — в `repdix:memory:{user_id}`. Объекты `asyncio.Lock` и `current_task` между процессами не переносимы, поэтому после рестарта незавершённый запрос не «докручивается», а `/stop` не может отменить задачу, запущенную до рестарта. Полная сериализация сессии — отдельная задача (`TODO(P1)` в `app/core/session.py`); до её решения потеря этого куска контекста при рестарте считается приемлемой.
- **Время ответа:** На сложный вопрос бот может сделать 5–10 SQL-запросов и потратить 30–60 секунд. Используйте `/stop`, если передумали.
- **Стоимость LLM:** Сложный анализ может стоить несколько центов. Бот автоматически ограничивает выгрузку строк (`LIMIT 10` для транскрибаций, `LIMIT 100` для остальных), чтобы не переполнять контекст. Для дополнительной экономии задайте `max_tokens` в `models.yaml`.
- **MySQL readonly + timeout:** при `readonly: true` для MySQL `MAX_EXECUTION_TIME` не ставится (взаимоисключающие `init_command`). В `execute_query` для MySQL добавлен клиентский `asyncio.wait_for(timeout=SQL_QUERY_TIMEOUT+2)` — бот не зависнет, но запрос продолжит выполняться на сервере до собственного завершения. Настоятельно рекомендуется использовать пользователя БД только с правами `SELECT` и таймаутом на уровне сервера.
- **MariaDB:** установите `is_mariadb: true`, иначе `SET SESSION MAX_EXECUTION_TIME` упадёт (aiomysql может проглотить ошибку, но лучше явно).
- **SQLite-комментарии без пробела:** `_strip_comments_for_check` в `validator.py` вырезает `--` только если после него есть пробел (или это начало строки). SQLite допускает `--no space` как комментарий — такие случаи могут дать ложноположительное срабатывание forbidden keywords. Редкий сценарий.
- **`SELECT *` + `excluded_columns` / `allowed_columns`:** RBAC запрещает `SELECT *`, если у таблицы есть скрытые поля **или** явный белый список полей. Модели нужно явно перечислять разрешённые поля.
- **Технические таблицы/колонки:** имена, начинающиеся с префиксов из `TECHNICAL_PREFIXES` (по умолчанию `technical_`), полностью скрыты от всех пользователей (включая админов): не попадают в промпт LLM, блокируются RBAC-гардом до применения правил из `roles.yaml`, вырезаются из результатов запросов, шаблонов и связей. Настраивается через env.
- **Контекст LLM:** при переполнении (> 80% лимита) история обрезается до 2 последних сообщений, при повторном переполнении — ошибка с просьбой использовать `/compact` или `/clear`. До 60% работает механизм Auto-Compact.
- **Hot-reload:** `models.yaml`, `databases.yaml`, `roles.yaml`, `users.yaml`, `relations.yaml`, `templates.yaml`, `help.md` перечитываются автоматически при изменении mtime (кроме `databases.yaml` — пулы соединений создаются один раз при старте).
- **Файлы конфигурации:** `config/users.yaml` содержит персональные данные (ФИО). **Обязательно** добавьте `config/users.yaml`, `.env` и `logs/` в `.gitignore`, чтобы не закоммитить их в публичный репозиторий.

## 🧪 10. Тестирование
Проект использует `pytest` + `pytest-asyncio`. Минимальный набор тестов:
- `tests/test_validator.py` — проверка `validate_sql` (разрешённые/запрещённые запросы, комментарии, multiple statements, null bytes, fail-closed парсинг).
- `tests/test_rbac_guard.py` — проверка `RBACGuard.validate_sql` (qualified-таблицы, `SELECT *` + excluded/allowed, обход через смену схемы, блокировка системных таблиц).
- `tests/test_rbac_guard_multi_schema.py` — проверка разворачивания правил `schema.*`.
- `tests/test_session.py` — проверка `SessionStore` (get/peek/clear, TTL, отмена task).
- `tests/test_user_manager.py` — проверка `UserManager` (add/approve/reject/delete, защита админов, hot-reload).
- `tests/test_technical_filter.py` — проверка фильтрации технических таблиц/колонок.
- `tests/test_telegram_sanitize.py` — проверка HTML-санитайзера (безопасные теги сохраняются, опасные — экранируются).

Запуск: `pytest -v`.

## 📋 11. Лицензия и контакты
Проект распространяется под лицензией **MIT**. Полный текст — в файле [`LICENSE.md`](LICENSE.md).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

- **Автор:** Дмитрий Неупокоев
- **GitHub:** [github.com/dneupokoev](https://github.com/dneupokoev)
- **Репозиторий проекта:** [github.com/dneupokoev/repdix](https://github.com/dneupokoev/repdix)