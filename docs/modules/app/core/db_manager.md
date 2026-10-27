# DBManager

## Назначение

Модуль `app/core/db_manager.py` предоставляет асинхронный менеджер подключений к базам данных MySQL, PostgreSQL и SQLite. Он отвечает за:

- загрузку конфигурации БД из YAML;
- подстановку переменных окружения вида `${VAR}`;
- создание и хранение пулов/соединений;
- выполнение SQL-запросов;
- получение списка таблиц и схемы таблиц;
- настройку таймаутов и read-only режима;
- безопасное закрытие соединений.

Версия файла: `261028.01`.

## Основной класс

### `DBManager`

```python
DBManager(config_path: str = "config/databases.yaml")
```

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
- подставляет `${VAR}` из окружения;
- если переменная окружения не найдена, записывает пустую строку и логирует warning.

### `get_db_names() -> list[str]`

Возвращает список имён БД из секции `databases`.

### `get_db_config(name: str) -> dict`

Возвращает конфиг указанной БД или пустой `dict`, если БД отсутствует.

### `get_db_type(name: str) -> str`

Возвращает тип БД в нижнем регистре: `mysql`, `postgresql` или `sqlite`.

### `async connect(name: str) -> None`

Создаёт пул соединений для указанной БД.

Аргументы:

- `name` — имя БД из конфигурации.

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
  - задаёт серверный `statement_timeout` в миллисекундах.

- **SQLite**:
  - открывает `aiosqlite` соединение;
  - при `readonly=True` выполняет `PRAGMA query_only = ON`;
  - для файловых БД пытается включить WAL;
  - выполняет `commit()`.

Исключения:

- `ValueError` — если БД нет в конфиге;
- `NotImplementedError` — если тип БД не поддерживается.

Если пул уже существует, повторно не создаётся.

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
- для PostgreSQL используется таймаут `asyncpg`;
- для SQLite используется `asyncio.wait_for` и корректное закрытие курсора;
- при таймауте SQLite выбрасывается `TimeoutError` с сообщением о превышении лимита.

### `async get_table_list(db_name: str) -> list[str]`

Возвращает список таблиц указанной БД.

Особенности:

- MySQL — использует `information_schema.tables` и текущую БД через `DATABASE()`, сортирует по имени;
- PostgreSQL — смотрит схему `public`, сортирует по имени;
- SQLite — читает `sqlite_master`, исключает системные таблицы `sqlite_%`.

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
- MySQL — читает `information_schema.columns`;
- PostgreSQL — читает `information_schema.columns` и комментарии из `pg_description`;
- SQLite — использует `PRAGMA table_info(...)`, при этом имя таблицы проверяется регуляркой `[A-Za-z0-9_]+`.

### `async close() -> None`

Закрывает все пулы/соединения.

Особенности:

- безопасно вызывать повторно;
- для объектов с `wait_closed` вызывает `close()` и `wait_closed()`;
- для `aiosqlite.Connection` выполняет `commit()` перед `close()`;
- неизвестные типы пытается закрыть через `close()`;
- ошибки закрытия логируются, но не прерывают закрытие остальных пулов.

## Зависимости

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

Модуль не импортирует другие модули проекта, поэтому явные зависимости от внутренних компонентов в изолированном файле не видны.

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

Значения вида `${VAR}` подставляются из переменных окружения.

## Ограничения и важные замечания

- MySQL `MAX_EXECUTION_TIME` поддерживается не всеми СУБД. Для MariaDB параметр не ставится.
- Для MySQL при `readonly=True` и не MariaDB выставляется `transaction_read_only`, но `MAX_EXECUTION_TIME` не устанавливается. Если нужны одновременно read-only и серверный таймаут, рекомендуется использовать пользователя БД только с правами `SELECT`.
- PostgreSQL `statement_timeout` действует на стороне сервера.
- SQLite read-only реализован через `PRAGMA query_only = ON`.
- Для SQLite имя таблицы в `PRAGMA table_info(...)` проверяется регуляркой, чтобы избежать небезопасной подстановки.
- Параметр `params` в `execute_query` уже поддерживается менеджером, но, согласно комментариям, engine может пока его не использовать.

## Использование другими модулями проекта

В изолированном файле нет импортов внутренних модулей проекта, поэтому точные связи не видны. По docstring и комментариям модуль рассчитан на использование из `engine`: предположительно engine импортирует `DBManager`, получает тип/конфиг БД через `get_db_type()` и `get_db_config()`, выполняет запросы через `execute_query()`, получает метаданные через `get_table_list()` и `get_table_schema()`, а при завершении вызывает `close()`.