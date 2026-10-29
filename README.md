# 📘 Проект: repdix (Reporting & Data eXplorer)

## 🎯 1. Цель проекта
Создать универсального Telegram-бота-оркестратора с системой модерации доступа, который принимает вопросы на естественном языке, переводит их в SQL-запросы через LLM, выполняет их в базе данных (MySQL, PostgreSQL, SQLite) и возвращает пользователю **глубокий аналитический ответ**.

Бот работает в режиме **AI-агента**: он умеет самостоятельно строить цепочки SQL-запросов (Agent Loop), находить скрытые закономерности (например, «почему упали продажи»), показывать процесс своих рассуждений в реальном времени (Chain of Thought) и прерывать выполнение по команде `/stop`.

## 🛠️ 2. Технологический стек
- **Язык:** Python 3.12.
- **Менеджер зависимостей:** Pipenv.
- **Асинхронность:** `asyncio`, `asyncio.Task` (для фоновых задач и отмены).
- **Telegram:** `aiogram` (v3.x) + `FSMContext` (для интервью при регистрации).
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
│   │   ├── engine.py           # ядро: Agent Loop, tool_calls, роутинг моделей, RBAC, формирование промпта
│   │   ├── session.py          # SessionState (история, lock, current_task, last_sqls) + SessionStore с TTL
│   │   ├── db_manager.py       # пулы соединений (MySQL/PostgreSQL/SQLite), таймауты, read-only, метаданные
│   │   ├── user_manager.py     # управление доступом (hot-reload users.yaml), add/approve/reject/delete
│   │   ├── llm_client.py       # клиент LLM (hot-reload models.yaml, temperature из конфига, fallback tools)
│   │   ├── cache_manager.py    # Redis (история диалогов, graceful degradation)
│   │   ├── rbac_guard.py       # проверка прав доступа к таблицам и полям (sqlglot AST)
│   │   ├── technical_filter.py # фильтрация технических таблиц/колонок (TECHNICAL_PREFIXES)
│   │   └── validator.py        # SQL-валидатор (только SELECT/WITH, forbidden keywords, комментарии)
│   └── interfaces/             # адаптеры мессенджеров
│       ├── __init__.py
│       ├── base.py             # ChatMessage (с колбэком статуса), ChatResponse, BaseChatInterface
│       ├── telegram_adapter.py # Telegram (aiogram), FSM-регистрация, статусы, /stop, модерация
│       └── cli_adapter.py      # Консольный интерфейс (отладка, CLI_USER_ID)
├── config/
│   ├── databases.yaml          # шаблоны подключений к целевым БД (${VAR} из .env)
│   ├── models.yaml             # шаблоны моделей LLM (temperature, context_length, supports_tools)
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

## 🗄️ 4. Работа с базами данных
- **Шаблоны в `config/databases.yaml`**: реальные значения подставляются из `.env` через `${VAR}`.
- **Read-only на уровне БД обязателен.** Валидатор SQL — лишь дополнительный барьер.
- **Таймауты:** `SQL_QUERY_TIMEOUT` (по умолчанию 15 секунд) прерывает тяжёлые запросы:
  - **MySQL:** `SET SESSION MAX_EXECUTION_TIME` (только MySQL 5.7.8+, не MariaDB). Ставится через `init_command` при создании пула.
  - **PostgreSQL:** `statement_timeout` в `server_settings` (прерывает запрос на стороне сервера).
  - **SQLite:** `asyncio.wait_for` вокруг `cursor.fetchall()`.
- **Read-only режим:**
  - **MySQL:** `SET SESSION transaction_read_only = 1` (при `readonly: true`, не MariaDB). ⚠️ При этом `MAX_EXECUTION_TIME` не ставится — таймаут на уровне БД не работает. Если нужны и readonly, и таймаут — используйте пользователя БД с правами только на SELECT.
  - **PostgreSQL:** `default_transaction_read_only = on` в `server_settings`.
  - **SQLite:** `PRAGMA query_only = ON`.
- **MariaDB:** установите `is_mariadb: true` в `databases.yaml`, чтобы отключить `MAX_EXECUTION_TIME` (MariaDB его не понимает).

## 🧠 5. Работа с моделями и AI-Агент

### 5.1. Agent Loop (Цикл агента)
В `engine.py` реализован цикл `while step < max_steps` (до 5 шагов). Модель может: изучить структуру таблиц (из системного промпта), выполнить несколько SQL-запросов, сравнить результаты и написать бизнес-вывод.
**Важно:** В цикле сообщения модели обязательно передаются с ролью `"tool"` и `tool_call_id`, иначе DeepSeek вернёт ошибку 400.

### 5.2. Chain of Thought (Прозрачность)
Перед каждым вызовом инструмента модель пишет краткое пояснение.
**Как это работает:** В `ChatMessage` есть `status_callback`. `telegram_adapter.py` отправляет эти статусы пользователю (вида `🤖 Сейчас посмотрю структуру...`). В `engine.py` текст перехватывается и отправляется через колбэк. Статусы автоматически обрезаются до 80 символов, чтобы не засорять чат. `cli_adapter.py` печатает статусы с отступом.

### 5.3. Инструменты модели (Tool Calling)
Доступны **только три** инструмента:
- `execute_sql` — для сложных аналитических SELECT-запросов.
- `get_relations` — узнать связи таблиц.
- `execute_template` — выполнить быстрый запрос из `config/templates.yaml` (только MySQL).
- **⚠️ Важно:** Мы **отказались** от `get_table_schema` (он возвращал гигантский JSON и переполнял контекст). Схема БД (таблицы + поля + связи) уже передаётся в системном промпте через `_get_db_schema()`. **Не возвращайте `get_table_schema` обратно!**
- **⚠️ Важно:** `SHOW COLUMNS` и `SHOW TABLES` **не пройдут** `validator.validate_sql` — только `SELECT` и `WITH`. Вся нужная информация о структуре уже в промпте.

### 5.4. Управление историей и прерывание
- История хранится в `SessionState.history` и дублируется в Redis (TTL 24 часа, `max_history = 10`).
- Выполненные SQL-запросы сохраняются отдельно в `SessionState.last_sqls` (до 20 штук) — они **не засоряют** `history` и используются только для подсказок LLM.
- **Как работает `/stop`:**
  - В `telegram_adapter.py` обработка запускается в `asyncio.create_task`. Ссылка сохраняется в `SessionState.current_task`.
  - Команда `/stop` вызывает `session_state.current_task.cancel()`. `asyncio.CancelledError` перехватывается в `run_and_reply` — пользователь получает сообщение «⛔ Выполнение прервано командой /stop».
  - Сессии хранятся в `SessionStore` (обычный `dict` с TTL по `last_access`, по умолчанию 24 часа). Сессия с активной задачей не удаляется по TTL.

## 🛡️ 6. Управление доступом (Модерация и RBAC)
Доступ к боту строго контролируется:
1. **Администраторы** (указаны в `ADMIN_USERS` в `.env` или имеют роль `admin` в `users.yaml`) имеют доступ ко всему. При старте `user_manager.sync_admins()` гарантирует, что все ID из `.env` имеют `is_active: true` и `roles: ["admin"]`.
2. **Обычные пользователи** при первом `/start` проходят интервью (ФИО, Отдел, Цель). Данные сохраняются в `config/users.yaml` (с флагом `is_active: false`). Пока админ не одобрит заявку, бот не пустит пользователя к LLM. **Файл перечитывается «на лету» (hot-reload по mtime) при каждом запросе, перезапуск не нужен.**
3. **Модерация:** админ получает уведомление с кнопками «✅ Одобрить» / «❌ Отклонить». После отклонения появляется кнопка «🔄 Сбросить статус» — она **удаляет** пользователя из `users.yaml`, чтобы он мог подать заявку заново (`reject_user` этого не делает).
4. **RBAC (Role-Based Access Control):** В `config/roles.yaml` описываются роли (например, `sales_manager`) и права на таблицы/поля. `RBACGuard` физически блокирует запросы к запрещённым таблицам и полям, даже если LLM сгенерирует такой SQL. Проверка идёт по полному имени `schema.table`; `SELECT *` запрещён, если у таблицы есть `excluded_columns`.
5. **Статусы:** `is_active: true` (доступ), `is_active: false` (бан/ожидание).
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
| `/compact` | Сжать историю (LLM-резюме, заменяет историю на одно system-сообщение). |
| `/clear` | Полностью очистить историю. |
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
    context_length: 32768
  deep_analysis_model:
    model_name: deepseek-v4.1-flash
    base_url: ${LLM_TOOL_URL}
    api_key: ${LLM_TOOL_TOKEN}
    supports_tools: false
    temperature: 0.2
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
`/help` читает файл `config/help.md`. Убедитесь, что в нём упомянуты все команды из раздела 7 (особенно `/stop`, `/models`, `/settings`, `/sql`).

## ⚠️ 9. Ограничения и известные проблемы
- **Одновременные запросы:** `asyncio.Lock` на каждого пользователя. Разные пользователи работают параллельно, один — последовательно.
- **Время ответа:** На сложный вопрос бот может сделать 5–10 SQL-запросов и потратить 30–60 секунд. Используйте `/stop`, если передумали.
- **Стоимость LLM:** Сложный анализ может стоить несколько центов. Бот автоматически ограничивает выгрузку строк (`LIMIT 10` для транскрибаций, `LIMIT 100` для остальных), чтобы не переполнять контекст.
- **MySQL readonly + timeout:** при `readonly: true` для MySQL `MAX_EXECUTION_TIME` не ставится (взаимоисключающие `init_command`). Если нужны оба — используйте пользователя БД с правами только на SELECT.
- **MariaDB:** установите `is_mariadb: true`, иначе `SET SESSION MAX_EXECUTION_TIME` упадёт (aiomysql может проглотить ошибку, но лучше явно).
- **Шаблоны (`execute_template`):** работают **только для MySQL** (используют `CURDATE()`, `DATE_SUB()`, `DATE_FORMAT()`). Для PostgreSQL/SQLite модель получит сообщение «Шаблоны доступны только для MySQL».
- **SQLite-комментарии без пробела:** `_strip_comments_for_check` в `validator.py` вырезает `--` только если после него есть пробел (или это начало строки). SQLite допускает `--no space` как комментарий — такие случаи могут дать ложноположительное срабатывание forbidden keywords. Редкий сценарий.
- **`SELECT *` + `excluded_columns`:** RBAC запрещает `SELECT *`, если у таблицы есть скрытые поля. Модели нужно явно перечислять разрешённые поля.
- **Технические таблицы/колонки:** имена, начинающиеся с префиксов из `TECHNICAL_PREFIXES` (по умолчанию `technical_`), полностью скрыты от всех пользователей (включая админов): не попадают в промпт LLM, блокируются RBAC-гардом до применения правил из `roles.yaml`, вырезаются из результатов запросов, шаблонов и связей. Настраивается через env.
- **Multi-schema:** одна MySQL-БД может содержать несколько схем. Список задаётся в `databases.yaml` (`schemas: auto` или явный список), исключения — `exclude_schemas`. Результат кэшируется на `SCHEMA_CACHE_TTL_SEC` (по умолчанию 3600 сек). Таблицы в промпте и в `get_table_list()` возвращаются fully-qualified (`schema.table`); `get_table_schema()` принимает `(db, table, schema)`. Схемы с техническими префиксами (`technical_*`) скрыты полностью — не попадают ни в промпт, ни в RBAC-проверки, ни в результаты.
- **Контекст LLM:** при переполнении (> 80% лимита) история обрезается до 2 последних сообщений, при повторном переполнении — ошибка с просьбой использовать `/compact` или `/clear`.
- **Hot-reload:** `models.yaml`, `databases.yaml`, `roles.yaml`, `users.yaml`, `relations.yaml`, `templates.yaml`, `help.md` перечитываются автоматически при изменении mtime (кроме `databases.yaml` — пулы соединений создаются один раз при старте).
- **Файлы конфигурации:** `config/users.yaml` содержит персональные данные (ФИО). **Обязательно** добавьте `config/users.yaml`, `.env` и `logs/` в `.gitignore`, чтобы не закоммитить их в публичный репозиторий.

## 🧪 10. Тестирование
Проект использует `pytest` + `pytest-asyncio`. Минимальный набор тестов:
- `tests/test_validator.py` — проверка `validate_sql` (разрешённые/запрещённые запросы, комментарии, multiple statements, null bytes).
- `tests/test_rbac_guard.py` — проверка `RBACGuard.validate_sql` (qualified-таблицы, `SELECT *` + excluded, обход через смену схемы).
- `tests/test_session.py` — проверка `SessionStore` (get/peek/clear, TTL, отмена task).
- `tests/test_user_manager.py` — проверка `UserManager` (add/approve/reject/delete, защита админов, hot-reload).

Запуск: `pytest -v`.

## 📋 11. Лицензия и контакты
Проект распространяется под лицензией **MIT**. Полный текст — в файле [`LICENSE.md`](LICENSE.md).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

- **Автор:** Дмитрий Неупокоев
- **GitHub:** [github.com/dneupokoev](https://github.com/dneupokoev)
- **Репозиторий проекта:** [github.com/dneupokoev/repdix](https://github.com/dneupokoev/repdix)
