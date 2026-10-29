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

Контракт взаимодействия адаптеров и ядра описан в [base](modules/app/interfaces/base.md): DTO `ChatMessage`/`ChatResponse` и абстрактный `BaseChatInterface`. `ChatMessage` содержит необязательный `status_callback`, а `ChatResponse` — `image_path` и `reply_markup`, который ядро передаёт адаптеру «как есть», не завися от `aiogram`.

Документация модулей сгруппирована по слоям: точка входа, ядро приложения и интерфейсы мессенджеров.

### Слои и поток обработки

- **Точка входа** загружает `.env`, настраивает логирование, инициализирует сервисы, создаёт движок и выбирает адаптер `cli` или `telegram`.
- **Адаптер** принимает входящее сообщение и передаёт в ядро нормализованный `ChatMessage`.
- **Ядро** выполняет маршрутизацию, проверки доступа и безопасности, оркестрирует LLM и запросы к БД, сохраняет историю и аудит, возвращает `ChatResponse`.
- **Инфраструктурные модули** отвечают за подключения к БД, LLM, кэш, пользователей, RBAC и валидацию SQL.

### Ключевые сквозные механизмы

- **Доступ и безопасность:** пользователи и роли, проверка RBAC по `roles.yaml`, валидация SQL и фильтрация технических сущностей — [user_manager](modules/app/core/user_manager.md), [rbac_guard](modules/app/core/rbac_guard.md), [validator](modules/app/core/validator.md), [technical_filter](modules/app/core/technical_filter.md).
- **Работа с данными:** пулы подключений к MySQL/PostgreSQL/SQLite, YAML-конфигурация, multi-schema-поддержка и кэш метаданных — [db_manager](modules/app/core/db_manager.md).
- **LLM-взаимодействие:** загрузка и hot-reload конфигурации моделей, подстановка `${VAR}`, выбор модели, лимиты контекста, tool-calling и retry-fallback — [llm_client](modules/app/core/llm_client.md).
- **Состояние и кэш:** in-memory сессии с TTL, блокировки и задача для `/stop`, Redis-кэш с graceful degradation — [session](modules/app/core/session.md), [cache_manager](modules/app/core/cache_manager.md).
- **Контракты адаптеров:** DTO входящих/исходящих сообщений и абстрактный интерфейс мессенджера — [base](modules/app/interfaces/base.md).
- **Наблюдаемость:** логи `audit.log` и `security.log`, аудит вопросов/ответов и SQL, фиксация нарушений SQL-безопасности — [main](modules/app/main.md), [engine](modules/app/core/engine.md), [validator](modules/app/core/validator.md).

## Модули проекта

### Точка входа

- [main](modules/app/main.md) — загрузка `.env`, настройка логирования (`audit.log`, `security.log` с ротацией), инициализация `DBManager`, `LLMClient`, `CacheManager`, `UserManager`, создание `RepdixEngine`, выбор адаптера `cli`/`telegram` и graceful shutdown.

### Ядро (`app/core`)

- [engine](modules/app/core/engine.md) — мессенджер-независимый диспетчер `ChatMessage`: обработка slash-команд (`/help`, `/db`, `/model`, `/models`, `/sql`, `/clear`, `/tokens`, `/history`, `/compact`, `/settings` и др.), детерминированные запросы по таблицам, LLM-агент с tool-calling до 5 шагов (`execute_sql`, `get_relations`, `execute_template`), multi-schema RBAC-фильтрация, аудит `manual`/`llm`, история диалога, кэш и защита от параллельных запросов через `state.lock`.
- [session](modules/app/core/session.md) — in-memory состояние пользователей в `dict` с TTL: история, выбранные БД/модель, `asyncio.Lock`, текущая задача для `/stop`, последние SQL, краткое резюме диалога и время последнего доступа. Очистка ленивая, примерно раз в 100 обращений к `SessionStore.get()`; сессии с активной задачей по TTL не удаляются.
- [db_manager](modules/app/core/db_manager.md) — асинхронные пулы MySQL/PostgreSQL/SQLite: загрузка YAML-конфигурации с подстановкой `${VAR}`, выполнение SQL и параметризованных запросов, таймауты, read-only, multi-schema поддержка (`schemas`, `exclude_schemas`, `description`), получение схем/таблиц/колонок, TTL-кэш метаданных, `technical_filter` и безопасное закрытие соединений.
- [user_manager](modules/app/core/user_manager.md) — управление `users.yaml` с hot-reload: заявки, одобрение/отклонение, роли, активность, env-админы, админ-панель и синхронизация администраторов.
- [llm_client](modules/app/core/llm_client.md) — асинхронная обёртка над `AsyncOpenAI`: загрузка `config/models.yaml`, hot-reload по mtime, подстановка `${VAR}`, выбор клиента/модели, лимиты контекста, `chat` с `temperature`, `max_tokens`, `tools`, `tool_choice`, `response_format`, строгая передача только положительного `max_tokens`, retry-fallback при неподдержке `tools`/`tool_choice` и закрытие всех клиентов.
- [cache_manager](modules/app/core/cache_manager.md) — асинхронный менеджер кэша на Redis с graceful degradation: `ping`/`healthcheck` при ошибке возвращают `False`, рабочие методы не бросают исключения; конструктор запрещает беспарольное подключение к не-loopback Redis для защиты от `CVE-2025-21605`, локальные подключения без пароля разрешены.
- [rbac_guard](modules/app/core/rbac_guard.md) — RBAC-проверка SQL по `roles.yaml`: полное имя `schema.table`, `default_schema`, ограничение `SELECT *` при `excluded_columns` и технических колонках, предварительная блокировка технических таблиц/колонок до RBAC-правил, разбор `sqlglot` с диалектом mysql и hot-reload.
- [technical_filter](modules/app/core/technical_filter.md) — определение технических имён по префиксам (`TECHNICAL_PREFIXES`, по умолчанию `technical_`), поддержка коротких и fully-qualified имён (`schema.table`, `schema.table.column`) без учёта регистра; используется для скрытия и блокировки технических сущностей.
- [validator](modules/app/core/validator.md) — предварительная fail-closed-проверка SQL через `sqlglot` с диалектом mysql: разрешает только `SELECT`/`WITH`, блокирует DML/DDL/административные конструкции, опасные паттерны, множественные запросы, нулевые байты и SQL длиннее `100_000` символов; возвращает причину отказа и очищенный SQL в verbose-варианте.

### Интерфейсы (`app/interfaces`)

- [base](modules/app/interfaces/base.md) — базовые DTO `ChatMessage`/`ChatResponse` и контракт `BaseChatInterface` с методами `start`, `send_message`, `send_image`; `reply_markup` передаётся адаптеру как есть, без зависимости ядра от `aiogram`.
- [telegram_adapter](modules/app/interfaces/telegram_adapter.md) — Telegram-адаптер на `aiogram 3`: FSM-регистрация, модерация заявок (одобрение/отклонение/сброс), админ-панель `/users` только для env-админов, per-chat меню команд, маршрутизация запросов в `RepdixEngine`, отправка длинных ответов/изображений и уведомлений, `/stop` для отмены анализа и корректное закрытие Telegram-сессии.
- [cli_adapter](modules/app/interfaces/cli_adapter.md) — консольный адаптер для отладки: ввод из `stdin`, вывод в `stdout`, статусы, служебные команды, отмена текущего запроса по `Ctrl+C` и работа с admin-ролью для RBAC.