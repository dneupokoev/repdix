# DBManager

## Назначение

Модуль `app/core/db_manager.py` предоставляет асинхронный менеджер подключений к базам данных MySQL, PostgreSQL и SQLite. Он отвечает за:

- загрузку конфигурации БД из YAML;
- подстановку переменных окружения вида `${VAR}`;
- создание и хранение пулов/соединений;
- выполнение SQL-запросов, в том числе параметризованных;
- получение списка таблиц и схемы таблиц;
- настройку таймаутов и read-only режима;
- скрытие технических сущностей через `technical_filter`;
- безопасное закрытие соединений.

Версия файла в коде: `261029.01`. В существующей документации была указана версия `261028.01` — актуальным считать значение из кода. В docstring перечислены ключевые изменения версии `261027.06`, но сам файл помечен версией `261029.01`.

## Зависимости и импорты

Стандартная библиотека и внешние пакеты:

- `asyncio`;
- `logging`;
- `os`;
- `re`;
- `typing.Any`;
- `yaml`;
- `aiosqlite`;
- `aiomysql`;
- `asyncpg`.

Внутренняя зависимость проекта:

- `from app.core.technical_filter import is_technical` — используется в `get_table_list()` и `get_table_schema()` для исключения технических таблиц и колонок.

Других импортов внутренних модулей проекта в изолированном файле нет.

## Основной класс

### `DBManager`

`DBManager(config_path: str = "config/databases.yaml")`

Управляет пулами соединений и конфигурацией БД.

Атрибуты:

- `config_path` — путь к YAML-файлу конфигурации;
- `pools` — словарь активных пулов/соединений по имени БД;
- `config` — загруженная конфигурация;
- `query_timeout` — таймаут SQL-запроса в секундах. Берётся из `SQL_QUERY_TIMEOUT`, по умолчанию `15`.

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

### `async connect(name: str) -> None`

Создаёт пул соединений для указанной БД.

Аргументы:

- `name` — имя БД из конфигурации.

Если пул уже существует, повторно не создаётся.

Поведение по типам БД:

- **MySQL**:
  - создаёт `aiomysql` pool;
  - использует `utf8mb4` по умолчанию;
  - `autocommit=True`;
  - `cursorclass=aiomysql.DictCursor`;
  - настраивает `init_command`:
    - `readonly=True` и не MariaDB — `SET SESSION transaction_read_only = 1`;
    - не readonly и не MariaDB — `SET SESSION MAX_EXECUTION_TIME=<timeout_ms>`;
    - MariaDB — `init_command=None`, так как `MAX_EXECUTION_TIME` не поддерживается.

- **PostgreSQL**:
  - создаёт `asyncpg` pool;
  - задаёт `command_timeout`;
  - при `readonly=True` включает `default_transaction_read_only=on`;
  - всегда задаёт серверный `statement_timeout` в миллисекундах через `server_settings`.

- **SQLite**:
  - открывает `aiosqlite` соединение;
  - при `readonly=True` выполняет `PRAGMA query_only = ON`;
  - для файловых БД пытается включить `PRAGMA journal_mode = WAL`;
  - выполняет `commit()`.

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
- для PostgreSQL используется `conn.fetch()` с таймаутом `asyncpg`, результат приводится к `list[dict]`;
- для SQLite используется `pool.execute()`, затем `asyncio.wait_for(cursor.fetchall(), timeout=...)`;
- для SQLite при таймауте выбрасывается `TimeoutError` с сообщением о превышении лимита;
- курсор SQLite закрывается в `finally`;
- для SQLite при отсутствии строк или `description is None` возвращается пустой список;
- параметр `params` уже поддержан менеджером, но, согласно комментариям, engine может пока его не использовать.

### `async get_table_list(db_name: str) -> list[str]`

Возвращает список таблиц указанной БД.

Особенности:

- при отсутствии пула автоматически вызывает `connect(db_name)`;
- MySQL — использует `information_schema.tables` и текущую БД через `DATABASE()`, сортирует по имени таблицы;
- PostgreSQL — смотрит схему `public`, сортирует по имени;
- SQLite — читает `sqlite_master`, исключает системные таблицы `sqlite_%`, сортирует по имени;
- во всех случаях технические имена фильтруются через `is_technical()`.

### `async get_table_schema(db_name: str, table_name: str) -> list[dict]`

Возвращает описание полей таблицы.

Аргументы:

- `db_name` — имя БД;
- `table_name` — имя таблицы.

Возвращаемые ключи:

- `column_name`;
- `data_type`;
- `column_comment`.

Особенности:

- при пустом `table_name` возвращает `[]`;
- при отсутствии пула автоматически вызывает `connect(db_name)`;
- MySQL — читает `information_schema.columns`, сортирует по `ordinal_position`, фильтрует технические колонки через `is_technical()`;
- PostgreSQL — читает `information_schema.columns` и комментарии из `pg_description`, фильтрует технические колонки через `is_technical()`;
- SQLite — использует `PRAGMA table_info(...)`, при этом имя таблицы проверяется регуляркой `[A-Za-z0-9_]+`;
- для SQLite комментарии колонок всегда пустые.

### `async close() -> None`

Закрывает все пулы/соединения.

Особенности:

- безопасно вызывать повторно;
- если `self.pools` пуст, сразу возвращается;
- для объектов с `wait_closed` вызывает `close()` и `wait_closed()`;
- для `aiosqlite.Connection` выполняет `commit()` перед `close()`;
- неизвестные типы пытается закрыть через `close()`;
- ошибки закрытия логируются, но не прерывают закрытие остальных пулов;
- после закрытия удаляет пулы из `self.pools` и очищает словарь.

## Конфигурация

Ожидается YAML-файл с корневой секцией `databases`, где каждая БД описана словарём.

Типовые параметры БД:

- `type` — `mysql`, `postgresql` или `sqlite`;
- `host`, `port`, `user`, `password`, `database` — для MySQL/PostgreSQL;
- `path` — для SQLite;
- `readonly` — включает read-only режим;
- `is_mariadb` — флаг для MySQL-совместимых MariaDB;
- `minsize`, `maxsize` — размеры пула;
- `charset` — кодировка MySQL.

Значения вида `${VAR}` подставляются из переменных окружения. Если переменная не найдена, подставляется пустая строка с записью warning в лог.

## Использование другими модулями проекта

В изолированном файле видна только одна внутренняя зависимость — импорт `is_technical` из `app.core.technical_filter`. Она используется для скрытия технических таблиц и колонок при получении метаданных.

По docstring и комментариям модуль рассчитан на использование из `engine`: предположительно engine импортирует `DBManager`, получает тип/конфиг БД через `get_db_type()` и `get_db_config()`, выполняет запросы через `execute_query()`, получает метаданные через `get_table_list()` и `get_table_schema()`, а при завершении вызывает `close()`. Точные вызовы из других модулей в этом изолированном файле не видны.

## Ограничения и важные замечания

- MySQL `MAX_EXECUTION_TIME` поддерживается не всеми СУБД. Для MariaDB параметр не ставится, если в конфиге указан `is_mariadb: true`.
- Для MySQL при `readonly=True` и не MariaDB выставляется `transaction_read_only`, но `MAX_EXECUTION_TIME` не устанавливается. Если нужны одновременно read-only и серверный таймаут, рекомендуется использовать пользователя БД только с правами `SELECT`.
- Для MariaDB при `is_mariadb: true` `init_command=None`: read-only и таймаут на уровне `init_command` не настраиваются. Read-only следует обеспечивать правами пользователя БД или другими слоями.
- Если `is_mariadb` не указан, а СУБД фактически является MariaDB, код при `readonly=False` может попытаться выполнить `SET SESSION MAX_EXECUTION_TIME=...`. Отдельного `try/except` вокруг `create_pool`/`init_command` в коде нет, поэтому такая ошибка не проглатывается автоматически.
- PostgreSQL `statement_timeout` действует на стороне сервера. Дополнительно используется `command_timeout` asyncpg.
- SQLite read-only реализован через `PRAGMA query_only = ON`.
- Для SQLite таймаут через `asyncio.wait_for` применяется к `cursor.fetchall()`, а не ко всей операции `execute()`.
- Для SQLite имя таблицы в `PRAGMA table_info(...)` проверяется регуляркой, чтобы избежать небезопасной подстановки.
- Технические таблицы и колонки скрываются через `is_technical()` из `app.core.technical_filter`.
- Параметр `params` в `execute_query` уже поддерживается менеджером, но, согласно комментариям, engine может пока его не использовать.