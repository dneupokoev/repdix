# Модуль проверки сложности SQL-запросов

## Назначение

Модуль `check_complexity` закрывает класс SQL-запросов, которые формально безопасны (проходят `validator.validate_sql` и `rbac_guard.validate_sql`), но потенциально «вешают» БД или взрывают контекст:

- слишком много `JOIN`;
- `JOIN` без условия (декартово произведение);
- глубокая вложенность подзапросов;
- слишком много `UNION`.

Проверка вызывается **после** проверок безопасности (`validator`, RBAC), но **до** `db_manager.execute_query()`. При превышении лимита бросается `SQLTooComplex`, который вызывающий код превращает в понятный отказ — текст исключения уже сформулирован для пользователя/LLM.

**Fail-closed:** если запрос не удалось разобрать `sqlglot` — это тоже `SQLTooComplex`. Невалидный/обфусцированный SQL не должен доходить до БД.

## Основные сущности

### `SQLTooComplex(Exception)`

Исключение, сигнализирующее об отклонении SQL проверкой сложности.

- Атрибут `reason: str` — человекочитаемая причина отказа (можно показать пользователю и передать LLM как результат инструмента).

### `_normalize_dialect(dialect: str) -> str`

Приводит имя диалекта к тому, что понимает `sqlglot`. Неизвестные значения возвращаются как есть — `sqlglot` выбросит `ValueError`, который `check_complexity` превратит в `SQLTooComplex` (fail-closed).

Поддерживаемые алиасы (`_DIALECT_ALIASES`):

| Входное значение | Диалект `sqlglot` |
|---|---|
| `mysql`, `mariadb` | `mysql` |
| `postgres`, `postgresql`, `pgsql` | `postgres` |
| `sqlite`, `sqlite3` | `sqlite` |

> MariaDB обслуживается диалектом `mysql` (отдельного диалекта MariaDB в `sqlglot` нет). `DBManager` возвращает `"postgresql"`, поэтому имя диалекта нормализуется.

### `_max_subquery_depth(node, depth=0) -> int`

Рекурсивно вычисляет максимальную глубину вложенности `exp.Subquery` в поддереве.

- `node: exp.Expression` — корень поддерева.
- `depth: int` — текущая глубина (сколько `Subquery` уже на пути от корня).

### `check_complexity(...)`

```python
check_complexity(
    sql: str,
    dialect: str = "mysql",
    max_joins: int = 5,
    max_depth: int = 3,
    max_unions: int = 3,
) -> None
```

Отклоняет слишком сложные SQL-запросы.

Аргументы:

- `sql` — SQL-запрос (только `SELECT`/`WITH` — гарантируется валидатором).
- `dialect` — диалект `sqlglot`: `"mysql"` (MySQL/MariaDB), `"postgres"` (PostgreSQL) или `"sqlite"`. `"postgresql"` нормализуется в `"postgres"`.
- `max_joins` — максимально допустимое количество `JOIN`.
- `max_depth` — максимальная глубина вложенности подзапросов.
- `max_unions` — максимально допустимое количество `UNION`.

Возвращает `None`, если запрос в пределах лимитов.

Бросает `SQLTooComplex`, если запрос не удалось разобрать (fail-closed) или превышен любой из лимитов. Текст исключения пригоден для показа пользователю.

Проверки выполняются в порядке:

1. Количество `JOIN` (через `tree.find_all(exp.Join)`).
2. Отсутствие условия у `JOIN` (`on`/`using`) — декартово произведение.
3. Глубина вложенности `Subquery` (`_max_subquery_depth`).
4. Количество `UNION` (`tree.find_all(exp.Union)`).

## Зависимости

Импорты из стандартной библиотеки и сторонних пакетов:

- `logging` — стандартное логирование (`logger`).
- `sqlglot` — парсинг SQL (`sqlglot.parse_one`).
- `sqlglot.errors` — обработка `sqlglot.errors.ParseError`.
- `sqlglot.exp` — типы узлов AST (`exp.Expression`, `exp.Subquery`, `exp.Join`, `exp.Union`).

Внутрипроектных импортов нет — модуль не зависит от других модулей проекта напрямую.

## Использование в проекте

Согласно docstring, модуль вызывается другими частями проекта (предположительно — инструментом выполнения запросов / исполнителем):

1. **После** `validator.validate_sql()` и `rbac_guard.validate_sql()`.
2. **До** `db_manager.execute_query()`.

`DBManager` предоставляет тип диалекта (`"mysql"` / `"postgresql"` / `"sqlite"`), который передаётся в `check_complexity` и нормализуется через `_normalize_dialect`.

При возникновении `SQLTooComplex` вызывающий код должен вернуть пользователю/LLM текст `reason` как понятный отказ.