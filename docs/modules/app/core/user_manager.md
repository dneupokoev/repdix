# `app/core/user_manager.py` — управление пользователями repdix

## Назначение

Модуль управляет базой пользователей проекта **repdix** через YAML-файл `config/users.yaml` (путь по умолчанию, можно переопределить в конструкторе). Предоставляет:

- чтение и запись пользователей с **hot-reload** по `mtime`;
- регистрацию заявок (`add_user`), одобрение/отклонение (`approve_user`/`reject_user`), удаление (`delete_user`);
- проверку прав (роль `admin`, активность, env-админы);
- методы для админ-панели: постраничный список, замена ролей, переключение активности, правка ФИО и заметок;
- синхронизацию админов из `.env` в `users.yaml` (`sync_admins`).

Версия файла: `261028.05`.

## Структура записи пользователя

Поля в `users.yaml` (см. `_USERS_HEADER`):

| Поле | Назначение |
|---|---|
| `id` | Telegram ID (строка из цифр) |
| `username` | Telegram-логин без `@` |
| `full_name` | ФИО, введённое при регистрации (не перезаписывается `/start`) |
| `telegram_name` | Имя из Telegram-профиля (обновляется при `/start`) |
| `department` | Отдел/должность из регистрации |
| `purpose` | Цель доступа |
| `is_active` | `true`/`false` — допущен ли пользователь |
| `roles` | Список ролей (`admin`, `sales_manager`, `viewer`, …) |
| `comment` | Заметки администратора |
| `meta` | Произвольный JSON (`db_login`, `branch_id`, …) |

## Класс `UserManager`

### Инициализация

```python
UserManager(users_yaml_path: str = "config/users.yaml")
```

- Кэширует пользователей в `self._users_cache` (dict `id -> user`).
- Список env-админов берётся из переменной окружения `ADMIN_USERS` (CSV).
- Имеет `asyncio.Lock` для защиты от гонок при параллельной записи.

### Чтение (синхронные)

| Метод | Описание |
|---|---|
| `get_user(user_id: str) -> dict \| None` | Данные пользователя по ID. |
| `get_user_roles(user_id: str) -> list[str]` | Список ролей (пустой, если пользователь не найден или роли не list). |
| `is_user_active(user_id: str) -> bool` | Активен ли пользователь. |
| `is_admin(user_id: str) -> bool` | `True` если в `ADMIN_USERS` ИЛИ имеет роль `admin` в YAML. |
| `is_env_admin(user_id: str) -> bool` | `True` только если входит в `ADMIN_USERS`. |
| `find_user(query: str) -> dict \| None` | Поиск по ID или `username` (регистронезависимо, `@` отбрасывается). |

- `_load_users_yaml() -> dict` — перечитывает файл при изменении `mtime`; при ошибке возвращает текущий кэш без сброса.

### Список пользователей

| Метод | Описание |
|---|---|
| `list_users(exclude_admins: bool = True) -> list[dict]` | Список пользователей. Сортировка: активные выше, затем по `full_name` (fallback — `username`/`id`). При `exclude_admins=True` исключает **env-админов**; YAML-админы остаются. |
| `list_users_paginated(page: int, per_page: int = 10, exclude_admins: bool = True) -> tuple[list[dict], int]` | Возвращает `(page_users, total)`. `page` — с нуля. |

### Сохранение

- `_save_users_yaml(users_data: dict) -> bool` — атомарная запись (tmp-файл + `os.replace`), проверка целостности записей (dict + обязательный `id`), обновляет кэш только после успешной записи. Возвращает `False` при ошибке (кэш не меняется).

### Изменение (все async, под `Lock`)

| Метод | Назначение |
|---|---|
| `add_user(user_id, username, full_name, department, purpose) -> bool` | Регистрация нового пользователя (`is_active=False`, `roles=[]`, `telegram_name=""`). **Не перезаписывает** env-админов и YAML-пользователей с ролью `admin` — возвращает `False`. |
| `approve_user(user_id, role="viewer", roles=None) -> bool` | `is_active=True`; роли **добавляются** к существующим. `roles` приоритетнее `role`. Логирует предупреждение, если итог — только `viewer`. |
| `reject_user(user_id) -> bool` | Ставит `is_active=False`. Запись не удаляется. |
| `delete_user(user_id) -> bool` | Полностью удаляет запись. env-админов удалять нельзя (`False`). |
| `update_user_profile(user_id, username, full_name) -> bool` | Обновляет `username` и `telegram_name`, **не трогает** `full_name`. Пустые значения не перезаписывают сохранённые. |
| `set_user_full_name(user_id, full_name) -> bool` | Ручная установка `full_name` (для админ-панели). |
| `set_user_roles(user_id, roles: list[str]) -> bool` | **Заменяет** роли (не добавляет). Пустой список — валиден. Дубликаты и пустые строки отбрасываются. env-админам менять запрещено. |
| `toggle_user_active(user_id) -> tuple[bool, bool]` | Возвращает `(ok, new_active)`. env-админов блокировать нельзя. |
| `update_user_notes(user_id, comment: str \| None = None, meta: dict \| None = None) -> bool` | Частичное обновление `comment` и `meta`. |
| `sync_admins() -> None` | Гарантирует, что все ID из `ADMIN_USERS` присутствуют в YAML, `is_active=True`, имеют роль `admin`. Создаёт отсутствующие записи с полной структурой полей. |

Поведение по умолчанию при «не найдено» / «невалидный ID» — `False` (или `(False, False)`, `None`, `[]`), лог `warning`.

### Валидация

`_validate_user_id(user_id) -> str | None` (статический) — `user_id` должен быть непустой строкой из цифр. Иначе `None` и запись отменяется.

## Ключевые принципы и защитные механизмы

- **Hot-reload**: кэш перечитывается при изменении `mtime` файла.
- **Атомарная запись** через `*.tmp` + `os.replace`. Временный файл удаляется при ошибке.
- **Сохранность кэша при ошибках**: при сбое чтения/записи YAML кэш в памяти **не сбрасывается**.
- **Защита админов**: методы не позволяют перезаписать/понизить/удалить/заблокировать пользователей из `ADMIN_USERS`. YAML-админы `add_user` тоже не перезаписывает (проверка роли `admin`).
- **Разделение полей**: `full_name` (из регистрации) и `telegram_name` (из Telegram) хранятся отдельно — исправляет прежний баг потери ФИО при `/start`.

## Зависимости

Импортируются только стандартная библиотека и `PyYAML`:

- `asyncio` — `Lock` для конкурентного доступа.
- `logging` — логгер `app.core.user_manager`.
- `os` — `environ` (`ADMIN_USERS`), `path`, `makedirs`, `replace`, `getmtime`, `remove`.
- `re` — валидация `user_id` (`\d+`), поиск по ID в `find_user`.
- `yaml` — `safe_load` / `dump` (с `allow_unicode=True`, `sort_keys=False`).

Внутренних зависимостей проекта модуль не импортирует.

## Использование другими модулями проекта

Исходя из назначения методов, модуль вызывается:

- **обработчиком `/start`** — `update_user_profile(...)` (обновление `username`/`telegram_name`), `get_user`, `is_user_active`, `is_admin` для проверки доступа;
- **сценарием регистрации** — `add_user(...)` при подаче заявки;
- **админ-панелью** — `list_users_paginated`, `set_user_roles`, `set_user_full_name`, `toggle_user_active`, `update_user_notes`, `approve_user`, `reject_user`, `delete_user`, `find_user`;
- **стартом приложения** — `sync_admins()` для приведения YAML в соответствие с `ADMIN_USERS`;
- **любым кодом проверки прав** — `is_admin`, `is_env_admin`, `is_user_active`, `get_user_roles`.

(Конкретные места вызова в этом файле не видны — приведено по назначению методов и комментариям.)

## Заметки автора (сохранено из кода)

- `add_user` создаёт запись с `telegram_name: ""`; это же делает `sync_admins`.
- `update_user_profile` приоритезирует сохранение `full_name`; пустые `username`/`full_name` не приводят к перезаписи.
- Пустой `roles=[]` в `set_user_roles` — **валидное** состояние (пользователь активен, но без доступа к таблицам).
- `viewer` не даёт доступа к таблицам — при одобрении с этой ролью логируется предупреждение.
- YAML-сортировка не сохраняется — порядок записи определяется порядком словаря, переданного в `_save_users_yaml`.