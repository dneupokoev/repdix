# repdix — Reporting & Data eXplorer

AI-аналитик (Telegram-бот-оркестратор аналитики): принимает вопрос пользователя на естественном языке, переводит его в SQL через LLM, выполняет запросы в базе данных и возвращает **глубокий аналитический ответ** в режиме AI-агента.

## Назначение

Проект решает задачи доступа к данным через естественно-языковой интерфейс. Бот:

- принимает вопросы на естественном языке;
- переводит их в SQL-запросы через LLM;
- выполняет запросы в MySQL, PostgreSQL или SQLite;
- возвращает аналитический ответ пользователю.

Работа построена в режиме **AI-агента**: бот самостоятельно строит цепочки SQL-запросов (Agent Loop), находит скрытые закономерности (например, «почему упали продажи»), отображает процесс рассуждений в реальном времени (Chain of Thought) и может быть остановлен командой `/stop`.

## Ключевые возможности

- **Agent Loop** — самостоятельное построение цепочек SQL-запросов.
- **Chain of Thought** — показ рассуждений в реальном времени.
- **Управление выполнением** — команда `/stop` для прерывания текущей задачи.
- **Модерация доступа** — система модерации и RBAC-разбор SQL через `sqlglot`.
- **Мульти-СУБД** — поддержка MySQL, PostgreSQL и SQLite.

## Технологический стек

- **Язык и зависимости:** Python 3.12, Pipenv.
- **Асинхронность:** `asyncio`, `asyncio.Task`.
- **Telegram:** `aiogram` v3.x + `FSMContext`.
- **LLM:** `openai` (AsyncOpenAI); основная модель — DeepSeek V4.1 Flash (OpenCode Zen) или локальные аналоги.
- **Драйверы БД:** `aiomysql`, `asyncpg`, `aiosqlite`.
- **Кэширование:** `redis.asyncio`.
- **Конфигурация:** `python-dotenv`, `PyYAML`.
- **Разбор SQL:** `sqlglot`.

## Архитектура

Проект построен по принципу **«Порты и Адаптеры»**: ядро не знает о Telegram, всё общение идёт через абстрактные интерфейсы. Благодаря этому бизнес-логика не зависит от конкретного мессенджера, а адаптеры подключаются через общий контракт.

Документация модулей сгруппирована по слоям: точка входа, ядро приложения и интерфейсы мессенджеров.

## Модули проекта

### Точка входа

- [main](modules/app/main.md) — загрузка `.env`, настройка логирования (`audit.log`, `security.log` с ротацией), инициализация `DBManager`, `LLMClient`, `CacheManager`, `UserManager`, создание `RepdixEngine`, выбор адаптера `cli`/`telegram` и graceful shutdown.

### Ядро (`app/core`)

- [engine](modules/app/core/engine.md) — мессенджер-независимый диспетчер `ChatMessage`: обработка slash-команд (`/help`, `/db`, `/model`, `/models`, `/sql`, `/clear`, `/tokens`, `/history`, `/compact`, `/settings` и др.), детерминированные запросы по таблицам, LLM-агент с tool-calling до 5 шагов (`execute_sql`, `get_relations`, `execute_template`), multi-schema RBAC-фильтрация, аудит `manual`/`llm`, история диалога, кэш и защита от параллельных запросов через `state.lock`.
- [session](modules/app/core/session.md) — in-memory состояние пользователей: история, выбранные БД/модель, `asyncio.Lock`, текущая задача для `/stop`, последние SQL и TTL-очистка неактивных сессий.
- [db_manager](modules/app/core/db_manager.md) — асинхронные пулы MySQL/PostgreSQL/SQLite: загрузка YAML-конфигурации с подстановкой `${VAR}`, выполнение SQL и параметризованных запросов, таймауты, read-only, multi-schema поддержка (`schemas`, `exclude_schemas`, `description`), получение схем/таблиц/колонок, TTL-кэш метаданных, `technical_filter` и безопасное закрытие соединений.
- [user_manager](modules/app/core/user_manager.md) — управление `users.yaml` с hot-reload: заявки, одобрение/отклонение, роли, активность, env-админы, админ-панель и синхронизация администраторов.
- [llm_client](modules/app/core/llm_client.md) — асинхронная обёртка над `AsyncOpenAI`: загрузка `models.yaml`, hot-reload, выбор модели, лимиты контекста, `chat`, `tools`, `tool_choice`, `response_format`, retry-fallback и закрытие клиентов.
- [cache_manager](modules/app/core/cache_manager.md) — обёртка над `redis.asyncio` с graceful degradation: `ping`/`healthcheck`, `get`/`set`/`delete`, TTL и безопасная работа при ошибках Redis.
- [rbac_guard](modules/app/core/rbac_guard.md) — RBAC-проверка SQL по `roles.yaml`: полное имя `schema.table`, `default_schema`, ограничение `SELECT *` при `excluded_columns` и технических колонках, предварительная блокировка технических таблиц/колонок до RBAC-правил, разбор `sqlglot` с диалектом mysql и hot-reload.
- [technical_filter](modules/app/core/technical_filter.md) — определение технических имён по префиксам (`TECHNICAL_PREFIXES`, по умолчанию `technical_`), поддержка коротких и fully-qualified имён (`schema.table`, `schema.table.column`) без учёта регистра; используется для скрытия и блокировки технических сущностей.
- [validator](modules/app/core/validator.md) — предварительная проверка безопасности SQL: разрешает только `SELECT`/`WITH`, блокирует DML/DDL, опасные паттерны, множественные запросы, нулевые байты и слишком длинный SQL.

### Интерфейсы (`app/interfaces`)

- [base](modules/app/interfaces/base.md) — базовые DTO `ChatMessage`/`ChatResponse` и контракт `BaseChatInterface` с методами `start`, `send_message`, `send_image`.
- [telegram_adapter](modules/app/interfaces/telegram_adapter.md) — Telegram-адаптер на `aiogram 3`: регистрация через FSM, модерация заявок, админ-панель `/users`, меню команд, маршрутизация в `RepdixEngine`, отправка длинных ответов/изображений, `/stop` и закрытие сессии.
- [cli_adapter](modules/app/interfaces/cli_adapter.md) — консольный адаптер для отладки: ввод из `stdin`, вывод в `stdout`, статусы, служебные команды, отмена текущего запроса по `Ctrl+C` и работа с admin-ролью для RBAC.
