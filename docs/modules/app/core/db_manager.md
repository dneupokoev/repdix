# DBManager

## Назначение

Модуль `app/core/db_manager.py` предоставляет асинхронный менеджер подключений к базам данных MySQL/MariaDB, PostgreSQL и SQLite. Он отвечает за:

- загрузку конфигурации БД из YAML;
- подстановку переменных окружения вида `${VAR}`;
- создание и хранение пулов/соединений;
- выполнение SQL-запросов, в том числе параметризованных;
- настройку таймаутов и read-only режима;
- поддержку нескольких схем в одной MySQL-БД через `schemas` (список или `"auto"`), `exclude_schemas`, `description`;
- получение списка схем, таблиц и описания полей таблиц;
- кэширование метаданных схемы/таблиц/колонок с TTL;
- скрытие технических сущностей через `technical_filter`;
- безопасное закрытие соединений.

Версия файла в коде: `261030.02`.

Ключевые изменения версии `261030.02`:

- Добавлен параметр `lock_wait_timeout`, читаемый из `LOCK_WAIT_TIMEOUT` (по умолчанию `5` секунд).
- Некорректное или неположительное значение `LOCK_WAIT_TIMEOUT` заменяется на `5` с записью warning в лог.
- MySQL/MariaDB: при автогенерации `init_statements` всегда добавляются:
  - `SET SESSION innodb_lock_wait_timeout = <lock_wait_timeout>`;
  - `SET SESSION lock_wait_timeout = <lock_wait_timeout>`.
- Ограничения ожидания блокировок применяются и для MySQL, и для MariaDB, и в режиме `readonly=true`.
- Явно заданный непустой `init_statements` по-прежнему полностью переопределяет автогенерацию, включая lock wait timeouts.

Ключевые изменения версии `261030.01`:

- MySQL/MariaDB: при автогенерации `init_statements` серверный таймаут ставится всегда, независимо от `readonly`.
- Для MariaDB используется `SET SESSION max_statement_time = <SQL_QUERY_TIMEOUT>` в секундах.
- Для MySQL используется `SET SESSION MAX_EXECUTION_TIME = <мс>`.
- `SET SESSION transaction_read_only = 1` по-прежнему ставится только при `readonly=true`.
- Явно заданный непустой `init_statements` полностью переопределяет автогенерацию.

Ключевые изменения версии `261029.04`:

- MySQL: `init_command` может содержать несколько SET-запросов через `"; "`, пул создаётся с `client_flag=CLIENT.MULTI_STATEMENTS`.
- MySQL: добавлен опциональный параметр конфига `init_statements` (список строк). Если он задан, используется вместо автогенерируемых SET.
- MySQL/MariaDB: при `readonly=true` ставится `transaction_read_only` и серверный таймаут. Для MariaDB без `readonly` серверный таймаут ранее не ставился; в `261030.01` это исправлено.

Ключевые изменения версии `261029.03`:

- MySQL: `execute_query` обёрнут в `asyncio.wait_for(timeout=SQL_QUERY_TIMEOUT+2)`.
- При срабатывании выбрасывается `TimeoutError` с тем же текстом, что и для SQLite.

Ключевые изменения версии `261029.02`:

- Поддержка нескольких схем в одной MySQL-БД: конфиг `schemas` (список или `"auto"`), `exclude_schemas`, `description`.
- Добавлены `get_schemas()`, `get_db_description()`, `get_db_display_name()`.
- `get_table_list()`/`get_table_schema()` принимают `schema=` и работают с fully-qualified именами `schema.table`. Это breaking change.
- Добавлен in-memory кэш схемы/таблиц/колонок с TTL (`SCHEMA_CACHE_TTL_SEC`, по умолчанию `3600`), `invalidate_schema_cache()`, очистка кэша в `close()`.

Ключевые изменения версии `261027.06`:

- MySQL: `MAX_EXECUTION_TIME` ставится через `init_command` при создании пула.
- PostgreSQL: добавлен `statement_timeout` в `server_settings`.
- SQLite: при `readonly=true` выполняется `PRAGMA query_only = ON`.
- Добавлен параметр `params` в `execute_query`.
- `close()` безопасно обрабатывает `aiosqlite.Connection`, у которого нет `wait_closed()`.
- `load_config()` не падает при отсутствии файла и проверяет тип `databases`.
- Добавлены `get_db_type()` и `get_db_config()`.

Важное breaking change версии `261029.02`: `get_table_list()` возвращает имена с префиксом схемы, а `get_table_schema()` ожидает отдельно `table_name` без схемы и `schema`.

## Зависимости и импорты

Стандартная библиотека:

- `asyncio`;
- `logging`;
- `os`;
- `re`;
- `time`;
- `typing.Any`.

Внешние пакеты:

- `yaml`;
- `aiosqlite`;
- `aiomysql`;
- `asyncpg`;
- `pymysql.constants.CLIENT`.

Внутренняя зависимость проекта:

- `from app.core.technical_filter import is_technical` — используется в `get_schemas()`, `get_table_list()` и `get_table_schema()` для исключения технических схем, таблиц и колонок.

Других импортов внутренних модулей проекта в изолированном файле нет.

## Основной класс

### `DBManager`

`DBManager(config_path: str = "config/databases.yaml")`

Управляет пулами соединений, конфигурацией БД и кэшем метаданных.

Атрибуты:

- `config_path` — путь к YAML-файлу конфигурации;
- `pools` — словарь активных пулов/соединений по имени БД;
- `config` — загруженная конфигурация;
- `query_timeout` — таймаут SQL-запроса в секундах. Берётся из `SQL_QUERY_TIMEOUT`, по умолчанию `15`;
- `schema_cache_ttl` — TTL кэша метаданных в секундах. Берётся из `SCHEMA_CACHE_TTL_SEC`, по умолчанию `3600`; при некорректном значении используется `3600`;
- `lock_wait_timeout` — таймаут ожидания блокировок MySQL/MariaDB в секундах. Берётся из `LOCK_WAIT_TIMEOUT`, по умолчанию `5`; при некорректном значении или значении `<= 0` используется `5` с записью warning в лог;
- `_schema_cache` — внутренний кэш метаданных: ключ-кортеж → `(monotonic_timestamp, data)`.

При инициализации вызывает `load_config()`.

## Кэш метаданных

Кэш используется для `get_schemas()`, `get_table_list()` и `get_table_schema()`.

- `_cache_get(key: tuple) -> Any | None` — возвращает значение, если оно есть и не устарело, иначе `None`. Устаревшая запись удаляется.
- `_cache_set(key: tuple, data: Any) -> None` — сохраняет значение с текущим `time.monotonic()`.
- `invalidate_schema_cache(db_name: str | None = None) -> None` — сбрасывает кэш. Если `db_name is None`, очищает весь кэш. Иначе удаляет все ключи, в которых встречается указанное имя БД.
- `close()` очищает `_schema_cache`, если `self.pools` не был пуст. Если `self.pools` пуст, `close()` сразу возвращается и кэш не очищает.

## Основные методы

### `load_config() -> None`

Загружает конфигурацию из YAML и подставляет значения переменных окружения.

Особенности:

- если файл не найден — пишет warning и оставляет пустой конфиг;
- при ошибке парсинга YAML — логирует ошибку и оставляет пустой конфиг;
- проверяет, что `databases` является словарём;
- если `databases` не словарь — логирует ошибку и устанавливает `config['databases'] = {}`;
- пропускает БД, конфиг которых не является словарём;
- подставляет `${VAR}` из окружения только для строк, целиком имеющих вид `${VAR}`;
- если переменная окружения не найдена, записывает пустую строку и логирует warning.

### `get_db_names() -> list[str]`

Возвращает список имён БД из секции `databases`. Если `databases` не словарь, возвращает пустой список.

### `get_db_config(name: str) -> dict`

Возвращает конфиг указанной БД или пустой `dict`, если БД отсутствует или конфиг имеет неверный тип.

### `get_db_type(name: str) -> str`

Возвращает тип БД в нижнем регистре: `mysql`, `postgresql` или `sqlite`. Если тип не задан, возвращает пустую строку.

### `get_db_description(name: str) -> str`

Возвращает `description` из конфига БД, обрезанный по краям. Если описания нет, возвращает пустую строку.

### `get_db_display_name(name: str) -> str`

Возвращает `"name — description"`, если описание задано, иначе просто `name`.

### `async get_schemas(db_name: str) -> list[str]`

Возвращает список схем (database) для указанной БД.

Логика:

- `schemas: [..]` — явный список. Пустые и технические значения отфильтровываются через `is_technical()`;
- `schemas: "auto"` — реально поддерживается только для MySQL: выполняется запрос к `information_schema.schemata`, исключаются схемы из `exclude_schemas` и технические схемы, результат сортируется;
- если `exclude_schemas` не задан списком, по умолчанию исключаются `mysql`, `information_schema`, `performance_schema`, `sys`;
- для PostgreSQL/SQLite значение `"auto"` приводит к fallback-логике;
- если `schemas` не задан:
  - для SQLite возвращается `["main"]`;
  - иначе возвращается `[conf["database"]]`, если значение непустое и не техническое;
  - иначе возвращается `[]`.

Успешный результат кэшируется. При ошибке SQL в режиме MySQL `auto` логируется warning и возвращается fallback без кэширования. Для не-`auto` режимов fallback кэшируется.

### `async connect(name: str) -> None`

Создаёт пул соединений для указанной БД.

Аргументы:

- `name` — имя БД из конфигурации.

Если пул уже существует, повторно не создаётся. Пул сохраняется только после успешного создания, поэтому после неудачной попытки следующий вызов может попробовать снова.

Поведение по типам БД:

- **MySQL/MariaDB**:
  - создаёт `aiomysql` pool;
  - использует `utf8mb4` по умолчанию;
  - `autocommit=True`;
  - `cursorclass=aiomysql.DictCursor`;
  - `client_flag=CLIENT.MULTI_STATEMENTS`;
  - `init_command` формируется из `init_statements` или автогенерируемых SET-запросов;
  - если `init_statements` задан непустым списком, он полностью переопределяет автогенерацию; пустые строки отбрасываются. Если после отбрасывания строк не осталось, `init_command` будет `None`;
  - если `init_statements` не задан или является пустым списком, выполняется автогенерация:
    - при `readonly=True` добавляется `SET SESSION transaction_read_only = 1`;
    - для MariaDB (`is_mariadb=True`) всегда добавляется `SET SESSION max_statement_time = <query_timeout>` в секундах;
    - для MySQL всегда добавляется `SET SESSION MAX_EXECUTION_TIME = <query_timeout * 1000>` в миллисекундах;
    - всегда добавляется `SET SESSION innodb_lock_wait_timeout = <lock_wait_timeout>`;
    - всегда добавляется `SET SESSION lock_wait_timeout = <lock_wait_timeout>`;
  - несколько SET-запросов объединяются через `"; "`;
  - размеры пула задаются через `minsize`/`maxsize` (по умолчанию `1`/`5`).

- **PostgreSQL**:
  - создаёт `asyncpg` pool;
  - задаёт `command_timeout`;
  - при `readonly=True` включает `default_transaction_read_only=on`;
  - всегда задаёт серверный `statement_timeout` в миллисекундах через `server_settings`;
  - размеры пула задаются через `minsize`/`maxsize` (по умолчанию `1`/`5`).

- **SQLite**:
  - открывает `aiosqlite` соединение;
  - при `readonly=True` выполняет `PRAGMA query_only = ON`;
  - для файловых БД пытается включить `PRAGMA journal_mode = WAL`; ошибки этого PRAGMA игнорируются;
  - выполняет `commit()`;
  - при ошибке инициализации логирует ошибку, закрывает соединение и пробрасывает исключение.

Исключения:

- `ValueError` — если БД нет в конфиге;
- `NotImplementedError` — если тип БД не поддерживается.

### `async execute_query(name: str, sql: str, params: tuple | None = None) -> list[dict]`

Выполняет SQL-запрос и возвращает список словарей.

Аргументы:

- `name` — имя БД;
- `sql` — SQL-запрос;
- `params` — необязательные параметры запроса.

Стиль параметров:

- MySQL — `%s`;
- PostgreSQL — `$1`;
- SQLite — `?`.

Особенности:

- при отсутствии пула автоматически вызывает `connect(name)`;
- для MySQL используется `DictCursor`, результат возвращается как список словарей;
- для MySQL запрос выполняется через `asyncio.wait_for` с таймаутом `query_timeout + 2`; при срабатывании выбрасывается `TimeoutError` с сообщением `Query timeout exceeded (<query_timeout> sec)`;
- для PostgreSQL используется `conn.fetch()` с таймаутом `asyncpg`, результат приводится к `list[dict]`; при наличии `params` они передаются как `*params`;
- для SQLite используется `pool.execute()`, затем `asyncio.wait_for(cursor.fetchall(), timeout=...)`;
- для SQLite при таймауте выбрасывается `TimeoutError` с сообщением о превышении лимита;
- курсор SQLite закрывается в `finally`;
- для SQLite при отсутствии строк или `description is None` возвращается пустой список;
- для SQLite `execute_query()` не выполняет `commit()` после запроса; `commit()` есть при инициализации соединения и в `close()`;
- параметр `params` уже поддержан менеджером, но, согласно комментариям, engine может пока его не использовать.

### `async get_table_list(db_name: str, schema: str | None = None) -> list[str]`

Возвращает список таблиц в виде fully-qualified имён `"schema.table"`.

Аргументы:

- `db_name` — имя БД из конфигурации;
- `schema` — конкретная схема. Если `None`, обходятся все схемы из `get_schemas(db_name)`.

Особенности:

- результат кэшируется; ключ кэша включает `schema or "*"`;
- если `schema is None`, список схем берётся из `get_schemas()`;
- для каждой схемы вызывается внутренний `_fetch_tables()`;
- ошибки по отдельной схеме логируются как warning, и обход продолжается; `NotImplementedError` пробрасывается;
- технические таблицы фильтруются через `is_technical()`;
- результат дедуплицируется и сортируется;
- имена формируются как `schema.table`; если схема пустая, имя остаётся без префикса.

Внутренний `_fetch_tables(db_name, schema) -> list[str]` возвращает короткие имена таблиц одной схемы без фильтрации:

- MySQL — `information_schema.tables` с `WHERE table_schema = %s ORDER BY table_name`;
- PostgreSQL — `information_schema.tables` с `WHERE table_schema = $1 ORDER BY table_name`;
- SQLite — `sqlite_master`, `type='table'`, исключая `sqlite_%`, с `ORDER BY name`; параметр `schema` в SQLite-запросе не используется, но в итоговом списке `get_table_list()` всё равно добавляет префикс переданной/вычисленной схемы.

### `async get_table_schema(db_name: str, table_name: str, schema: str | None = None) -> list[dict]`

Возвращает описание полей таблицы.

Аргументы:

- `db_name` — имя БД;
- `table_name` — имя таблицы без схемы;
- `schema` — схема. Если `None`, берётся `conf["database"]`; для SQLite принудительно используется `"main"`.

Возвращаемые ключи:

- `column_name`;
- `data_type`;
- `column_comment`.

Особенности:

- при пустом `table_name` возвращает `[]`;
- результат кэшируется; ключ кэша включает схему и имя таблицы;
- MySQL — читает `information_schema.columns`, сортирует по `ordinal_position`, фильтрует технические колонки через `is_technical()`;
- PostgreSQL — читает `information_schema.columns` и комментарии из `pg_description` через `pg_statio_all_tables`, сортирует по `ordinal_position`, фильтрует технические колонки через `is_technical()`;
- SQLite — при отсутствии пула вызывает `connect(db_name)`, проверяет `table_name` регуляркой `[A-Za-z0-9_]+`, использует `PRAGMA table_info(...)`, фильтрует технические колонки через `is_technical()`; комментарии колонок всегда пустые; параметр `schema` в `PRAGMA` не используется;
- для неподдерживаемого типа БД выбрасывается `NotImplementedError`.

### `async close() -> None`

Закрывает все пулы/соединения.

Особенности:

- если `self.pools` пуст, сразу возвращается;
- безопасно вызывать повторно;
- для объектов с `wait_closed` вызывает `close()` и `wait_closed()`;
- для `aiosqlite.Connection` выполняет `commit()` перед `close()`;
- неизвестные типы пытается закрыть через `close()`, при необходимости ожидая coroutine;
- ошибки закрытия логируются, но не прерывают закрытие остальных пулов;
- после закрытия удаляет пулы из `self.pools` и очищает `self.pools`;
- если `self.pools` был непустым, очищает `_schema_cache`.

## Конфигурация

Ожидается YAML-файл с корневой секцией `databases`, где каждая БД описана словарём.

Типовые параметры БД:

- `type` — `mysql`, `postgresql` или `sqlite`;
- `host`, `port`, `user`, `password`, `database` — для MySQL/PostgreSQL;
- `path` — для SQLite;
- `readonly` — включает read-only режим;
- `is_mariadb` — флаг для MySQL-совместимых MariaDB;
- `init_statements` — список SQL-операторов для `init_command` MySQL. Если задан непустым, заменяет автогенерацию;
- `minsize`, `maxsize` — размеры пула;
- `charset` — кодировка MySQL;
- `schemas` — список схем или `"auto"` для MySQL;
- `exclude_schemas` — список схем, исключаемых в MySQL-режиме `schemas: "auto"`;
- `description` — описание БД для `get_db_description()` и `get_db_display_name()`.

Переменные окружения:

- `SQL_QUERY_TIMEOUT` — таймаут SQL-запроса в секундах, по умолчанию `15`;
- `SCHEMA_CACHE_TTL_SEC` — TTL кэша метаданных в секундах, по умолчанию `3600`;
- `LOCK_WAIT_TIMEOUT` — таймаут ожидания блокировок MySQL/MariaDB в секундах, по умолчанию `5`.

Значения вида `${VAR}` подставляются из переменных окружения. Если переменная не найдена, подставляется пустая строка с записью warning в лог.

## Использование другими модулями проекта

В изолированном файле видна только одна внутренняя зависимость — импорт `is_technical` из `app.core.technical_filter`. Она используется для скрытия технических схем, таблиц и колонок при получении метаданных.

Прямых импортов `engine` или других модулей проекта в этом файле нет. По docstring и комментариям модуль рассчитан на использование из `engine`: предположительно engine импортирует `DBManager`, получает тип/конфиг БД через `get_db_type()` и `get_db_config()`, выполняет запросы через `execute_query()`, получает метаданные через `get_schemas()`, `get_table_list()` и `get_table_schema()`, а при завершении вызывает `close()`. Точные вызовы из других модулей в этом изолированном файле не видны.

## Ограничения и важные замечания

- MySQL `MAX_EXECUTION_TIME` поддерживается не всеми СУБД. Для MariaDB используется `max_statement_time` в секундах.
- Если `is_mariadb` не указан, а СУБД фактически является MariaDB, код при автогенерации попытается выполнить `SET SESSION MAX_EXECUTION_TIME=...`. Отдельного `try/except` вокруг `create_pool`/`init_command` в коде нет, поэтому такая ошибка не проглатывается автоматически.
- В версии `261030.01` при автогенерации MySQL/MariaDB серверный таймаут ставится всегда, независимо от `readonly`. `transaction_read_only` ставится только при `readonly=true`.
- В версии `261030.02` при автогенерации MySQL/MariaDB всегда ставятся `innodb_lock_wait_timeout` и `lock_wait_timeout` на уровне сессии.
- Явно заданный непустой `init_statements` полностью переопределяет автогенерацию, включая установку read-only, серверного таймаута и lock wait timeouts. Если в таком списке нет нужных SET-запросов, соответствующие настройки применены не будут.
- MySQL использует `client_flag=CLIENT.MULTI_STATEMENTS`, поэтому несколько SET-запросов можно объединять через `"; "`.
- Для MySQL дополнительно действует клиентский таймаут `asyncio.wait_for` с `query_timeout + 2`.
- PostgreSQL `statement_timeout` действует на стороне сервера. Дополнительно используется `command_timeout` asyncpg и таймаут в `conn.fetch()`.
- Если для PostgreSQL не задан `schemas`, `get_schemas()` использует fallback `[conf["database"]]`, а не `public`. Для получения таблиц PostgreSQL обычно нужно явно задать `schemas: ["public"]` или передать `schema="public"`.
- SQLite read-only реализован через `PRAGMA query_only = ON`.
- Для SQLite таймаут через `asyncio.wait_for` применяется к `cursor.fetchall()`, а не ко всей операции `execute()`.
- Для SQLite имя таблицы в `PRAGMA table_info(...)` проверяется регуляркой, чтобы избежать небезопасной подстановки. Параметр `schema` в SQLite-запросах метаданных не используется.
- Технические схемы, таблицы и колонки скрываются через `is_technical()` из `app.core.technical_filter`.
- Параметр `params` в `execute_query` уже поддерживается менеджером, но, согласно комментариям, engine может пока его не использовать.
- `get_table_list()` возвращает fully-qualified имена `schema.table`, это breaking change. `get_table_schema()` при этом ожидает `table_name` без схемы и отдельный параметр `schema`.
- Кэш метаданных имеет TTL по умолчанию `3600` секунд. После DDL-изменений стоит вызывать `invalidate_schema_cache(db_name)`.
- `close()` при пустом `self.pools` сразу возвращается и не очищает `_schema_cache`; при непустом `self.pools` кэш очищается.
- `SQL_QUERY_TIMEOUT` читается без `try/except`; некорректное значение приведёт к исключению при инициализации `DBManager`.
- `LOCK_WAIT_TIMEOUT` и `SCHEMA_CACHE_TTL_SEC` при некорректных значениях используют безопасные значения по умолчанию (`5` и `3600` соответственно).