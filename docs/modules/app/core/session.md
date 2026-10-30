# app/core/session.py — состояние сессий пользователей

Версия файла: `261030.01`

## Назначение

Модуль хранит in-memory состояние сессии для каждого пользователя: историю диалога, выбранную БД/модель, `asyncio.Lock` для последовательной обработки запросов, ссылку на текущую фоновую задачу для `/stop`, последние SQL, краткое резюме диалога, долговременные факты и время последнего доступа.

Сессии хранятся в обычном `dict` с TTL. TTL нужен для автоматического удаления неактивных сессий. Ленивая очистка выполняется из `SessionStore.get()` примерно раз в 100 обращений, без отдельной фоновой задачи. Сессии с активной задачей по TTL не удаляются, чтобы `/stop` оставался надёжным.

Модуль не импортирует Redis и другие модули проекта. При этом по docstring движок может сохранять часть полей сессии во внешнее хранилище: `summary` и `last_sqls` — в Redis-ключ `session:{user_id}` вместе с `current_db`/`current_model` через `engine._persist_session`, а `long_term_facts` — в `repdix:memory:{user_id}` с TTL 30 дней.

## Основные классы и функции

### `SessionState`

Dataclass с состоянием одного пользователя.

Поля:

- `user_id: str` — Telegram/CLI ID пользователя.
- `history: list[dict] = []` — история диалога.
- `current_db: str | None = None` — выбранная БД из `databases.yaml`.
- `current_model: str | None = None` — явно выбранная модель; `None` означает авто-роутер.
- `last_model_used: str = "none (deterministic)"` — последняя использованная модель для `/settings` и аудита.
- `lock: asyncio.Lock` — lock на пользователя, чтобы один запрос выполнялся за раз. Создаётся через `field(default_factory=asyncio.Lock)` при конструировании `SessionState`. Копировать/сериализовать `SessionState` с `lock` нельзя.
- `current_task: asyncio.Task | None = None` — ссылка на фоновую задачу для `/stop`.
- `last_sqls: list[str] = []` — последние выполненные SQL; хранятся отдельно от `history`, чтобы не засорять контекст.
- `summary: str = ""` — краткое резюме старого диалога для auto-compact.
- `long_term_facts: list[str] = []` — факты долговременной памяти. По актуальному docstring модуля это рабочая долговременная память, используемая движком; сам `SessionStore` логикой памяти не управляет.
- `last_access: float` — `time.monotonic()` последнего обращения; используется для TTL.

### `SessionStore`

Хранилище сессий с TTL и ленивой очисткой.

`__init__(default_db: str | None = None, ttl_sec: int = _DEFAULT_TTL_SEC)`

- `default_db` — БД по умолчанию для новых сессий.
- `ttl_sec` — TTL сессии в секундах; принудительно не меньше `60`.
- TTL по умолчанию: `SESSION_TTL_SEC` из окружения или `24 * 3600` (24 часа, как TTL истории в Redis).
- Внутри хранится счётчик обращений `_access_counter`; очистка запускается раз в `_cleanup_every = 100` обращений.

Методы:

- `get(user_id: str) -> SessionState` — возвращает сессию, создавая её при необходимости. Приводит `user_id` к строке, обновляет `last_access`, лениво запускает `_cleanup_expired(now)` раз в 100 обращений.
- `peek(user_id: str) -> SessionState | None` — возвращает сессию без создания и без обновления `last_access`. Полезно для `/stop`.
- `touch(user_id: str) -> None` — обновляет `last_access`, если сессия существует. Нужно для длинных операций, чтобы TTL не сработал.
- `clear(user_id: str) -> None` — удаляет сессию и отменяет её `current_task`, если задача ещё не завершена.
- `clear_all() -> None` — очищает все сессии; используется для graceful shutdown и тестов.
- `_cleanup_expired(now: float) -> int` — приватная очистка: удаляет сессии, к которым не обращались дольше TTL, кроме сессий с активной задачей. Возвращает количество удалённых сессий.
- `stats() -> dict` — диагностика: количество сессий, активных задач, TTL и возраст самой старой сессии.

## Зависимости

Импортируются только стандартные модули:

- `asyncio` — `Lock`, `Task`.
- `logging` — логгер модуля.
- `os` — чтение `SESSION_TTL_SEC`.
- `time` — `time.monotonic()`.
- `dataclasses` — `dataclass`, `field`.

Импортов других модулей проекта в файле нет.

## Использование в проекте

Точные импорты и вызовы из других модулей по этому файлу не видны. По комментариям и API модуль предназначен для интеграции в:

- обработчики запросов — получение `SessionState` через `get()` и использование `lock` для сериализации запросов пользователя;
- `/stop` — проверка `peek()` и отмена `current_task`;
- `/clear` — вызов `clear()`;
- `/settings` — использование `stats()`;
- engine — хранение последних SQL в `last_sqls` отдельно от `history`;
- долговременную память и auto-compact — поля `summary` и `long_term_facts` в `SessionState`.

По docstring модуля:

- `summary` и `last_sqls` сохраняются в Redis-ключе `session:{user_id}` вместе с `current_db`/`current_model` через `engine._persist_session`;
- `long_term_facts` используются engine как долговременная память в Redis `repdix:memory:{user_id}` с TTL 30 дней;
- `long_term_facts` загружаются в state при входе в `process_message` и через `_load_memory` в callback-хэндлерах адаптера.

## Заметки по версии 261030.01

- `summary` и `last_sqls` сохраняются в Redis-ключе `session:{user_id}` вместе с `current_db`/`current_model` через `engine._persist_session`.
- Docstring приведён в соответствие с кодом:
  - `cleanup()` заменён на `_cleanup_expired()`, который вызывается лениво из `get()` раз в `_cleanup_every` обращений;
  - `lock` — обычное dataclass-поле с `default_factory=asyncio.Lock`, а не ленивое property;
  - `long_term_facts` больше не «задел», а рабочая долговременная память.

## Заметки по версии 261027.05

- `WeakValueDictionary` заменён на обычный `dict` с TTL, чтобы сессия не терялась между сообщениями и не пропадали `current_task`, `lock` и `current_db`.
- Добавлен TTL: сессия удаляется, если к ней не обращались дольше `SESSION_TTL_SEC`.
- `touch()` обновляет `last_access`; `get()` делает это автоматически.
- Очистка просроченных сессий выполняется лениво из `get()`, а не отдельной фоновой задачей.
- Добавлен `clear_all()` для graceful shutdown и тестов.
- В `SessionState` добавлены `last_sqls` и `last_access`.
- `SessionState.lock` создаётся при конструировании `SessionState`; `SessionState` с lock нельзя копировать или сериализовать.

## Примечания о расхождениях с документацией

В старых версиях документации отмечались расхождения с docstring:

- упоминался публичный `cleanup()`, но в коде есть только приватный `_cleanup_expired(now)`, вызываемый из `get()`;
- указывалось, что `SessionState.lock` создаётся лениво через property, но в коде `lock` — dataclass-поле с `default_factory=asyncio.Lock`;
- упоминались поля `summary` и `long_term_facts`; в текущем коде они присутствуют в `SessionState`.

В версии `261030.01` эти пункты синхронизированы с кодом.