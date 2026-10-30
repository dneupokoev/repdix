# app/main.py

## Назначение
`app/main.py` — точка входа приложения **repdix**. Модуль:
- настраивает консольное логирование и файловые логи `audit.log` / `security.log` с ротацией;
- загружает переменные окружения из `.env`;
- инициализирует `DBManager`, `LLMClient`, `CacheManager`, `UserManager`;
- создаёт `RepdixEngine`;
- выбирает и запускает адаптер `cli` или `telegram`;
- выполняет graceful shutdown при завершении или ошибке.

Запуск предусмотрен как скрипт: `asyncio.run(main())`.

## Версия
`dv_file_version = '261028.10'`.

Docstring модуля: `"""Точка входа repdix: выбор адаптера и запуск event loop"""`.  
В текущем коде версия при старте не логируется. Упоминания об изменениях версии `261028.08` в docstring нет — это нужно учитывать как устаревшее/неподтверждённое кодом замечание.

## Основные функции

### `_setup_logging() -> None`
Настраивает логирование:
- вызывает `logging.basicConfig` с уровнем `INFO` и форматом `%(asctime)s [%(levelname)s] %(name)s: %(message)s`;
- создаёт директорию `logs` через `os.makedirs(..., exist_ok=True)`;
- при `OSError` логирует ошибку и warning, после чего файловые audit/security-логи отключаются; консольное логирование остаётся;
- настраивает логгер `audit`:
  - `RotatingFileHandler` для `logs/audit.log`;
  - `delay=True`, `encoding='utf-8'`, формат `%(message)s`, `propagate=False`;
  - размер ротации из env `LOG_MAX_BYTES` и `LOG_BACKUP_COUNT`, по умолчанию 100 MB × 5;
- настраивает логгер `security`:
  - `RotatingFileHandler` для `logs/security.log`;
  - `delay=True`, `encoding='utf-8'`, формат `%(message)s`, `propagate=False`;
  - размер ротации из env `SECURITY_LOG_MAX_BYTES` и `SECURITY_LOG_BACKUP_COUNT`, по умолчанию 50 MB × 5.

Числовые env-переменные приводятся через `int(...)`; при некорректном значении будет исключение.

### `async _init_services() -> tuple[DBManager, LLMClient, CacheManager, UserManager]`
Создаёт и инициализирует сервисы:
- `DBManager()` — без аргументов;
- `LLMClient(default_model=os.environ.get("DEFAULT_MODEL", "tool_calling_model"))`;
- `CacheManager(host=..., port=..., password=...)`:
  - `REDIS_HOST` по умолчанию `localhost`;
  - `REDIS_PORT` по умолчанию `6379`, приводится через `int(...)`;
  - `REDIS_PASSWORD` по умолчанию `None`;
- вызывает `await cache_manager.ping()`:
  - при успехе логирует `Redis: подключение OK`;
  - при `False` пишет warning и продолжает запуск;
  - исключение из `ping()` внутри `_init_services()` не перехватывается и приведёт к обработке в `main()`;
- создаёт `UserManager()` и вызывает `await user_manager.sync_admins()`;
- возвращает кортеж инициализированных сервисов.

### `_build_adapter(engine: RepdixEngine)`
Создаёт адаптер по env `ADAPTER`:
- читает `ADAPTER`, по умолчанию `cli`; приводит значение к нижнему регистру;
- логирует исходное и эффективное значение `ADAPTER`, наличие `TELEGRAM_TOKEN`, значение `CLI_USER_ID` и предупреждение про особенности `pipenv run` / `.env`;
- для `telegram`:
  - при заданном `CLI_USER_ID` логирует warning;
  - если `TELEGRAM_TOKEN` не задан — логирует ошибку и возвращает `None`;
  - иначе возвращает `TelegramAdapter(engine)`;
- для `cli` возвращает `CLIAdapter(engine)`;
- для неизвестного значения логирует ошибку и возвращает `CLIAdapter(engine)` как fallback.

### `async main() -> None`
Основной сценарий запуска:
- сначала вызывает `_setup_logging()`, затем логирует `ADAPTER` до `load_dotenv()`;
- вызывает `load_dotenv(override=False)`: явные env-переменные не перезаписываются;
- логирует `ADAPTER` после `load_dotenv()` и сообщение `repdix запускается`;
- инициализирует сервисы через `_init_services()`; при исключении логирует его и завершает процесс с `sys.exit(1)`;
- создаёт `RepdixEngine` с аргументами:
  - `db_manager`;
  - `llm_client`;
  - `cache_manager`;
  - `default_db=os.environ.get("DEFAULT_DB")`;
  - `user_manager`;
- создаёт адаптер через `_build_adapter(engine)` и логирует его тип;
- если адаптер не создан — логирует ошибку, вызывает `_shutdown(engine, None)` и завершает процесс с `sys.exit(1)`;
- запускает `await adapter.start()`;
- перехватывает `KeyboardInterrupt` / `asyncio.CancelledError` и другие исключения;
- в `finally` всегда вызывает `_shutdown(engine, adapter)`.

### `async _shutdown(engine: RepdixEngine, adapter) -> None`
Аккуратно закрывает ресурсы:
- вызывает `engine.sessions.clear_all()` синхронно, без `await`; ошибки логирует warning;
- закрывает `engine.db_manager` через `await close()`;
- закрывает `engine.llm_client`, если он задан;
- закрывает `engine.cache_manager`, если он задан;
- закрывает адаптер через `await adapter.close()`, если у адаптера есть `close`;
- все ошибки закрытия перехватываются и логируются warning, чтобы shutdown не падал;
- логирует `repdix остановлен`.

## Зависимости

### Стандартная библиотека
- `asyncio`
- `logging`
- `logging.handlers`
- `os`
- `sys`
- `logging.handlers.RotatingFileHandler`

### Внешние зависимости
- `dotenv.load_dotenv`

### Модули проекта
- `app.core.cache_manager.CacheManager`
- `app.core.db_manager.DBManager`
- `app.core.engine.RepdixEngine`
- `app.core.llm_client.LLMClient`
- `app.core.user_manager.UserManager`
- `app.interfaces.cli_adapter.CLIAdapter`
- `app.interfaces.telegram_adapter.TelegramAdapter`

## Использование другими модулями
В представленном файле видно, что `app/main.py` сам импортирует и связывает модули `app.core.*` и `app.interfaces.*`, но не видно, чтобы другие модули импортировали `app.main`. Практически это точка входа приложения, запускаемая напрямую:
- `python -m app.main` — основной рекомендуемый вариант запуска из корня проекта;
- `.venv/bin/python -m app.main` — вариант из комментариев в коде;
- прямой запуск `python app/main.py` возможен при корректно настроенном `PYTHONPATH`/установленном пакете.

Другие модули используются внутри `main.py` через создание объектов и вызовы:
- `CacheManager.ping()`, `CacheManager.close()`;
- `UserManager.sync_admins()`;
- `DBManager.close()`;
- `LLMClient.close()`;
- `RepdixEngine(...)`, `engine.sessions.clear_all()`;
- `adapter.start()`, `adapter.close()`.

## Поведение и ограничения
- При неизвестном `ADAPTER` приложение не завершается, а переключается на `CLIAdapter` после `logger.error`.
- При `ADAPTER=telegram` без `TELEGRAM_TOKEN` адаптер не создаётся, выполняется shutdown и процесс завершается с кодом `1`.
- Проверка Redis в комментарии названа fail-fast, но фактически при `ping() == False` приложение только пишет warning и продолжает запуск; исключение из `ping()` не перехватывается в `_init_services()` и приводит к выходу через обработку в `main()`.
- Числовые env-переменные приводятся через `int(...)`; при некорректном значении будет исключение.
- Если директорию `logs` создать не удалось, файловые audit/security-логи отключаются, консольное логирование остаётся.
- Shutdown устойчив к ошибкам: исключения при закрытии ресурсов логируются, но не прерывают завершение.
- Собственных классов модуль не определяет; все классы импортируются из других модулей.
- `_shutdown()` вызывает `engine.sessions.clear_all()` синхронно, без `await`.
- `_setup_logging()` вызывается до `load_dotenv()`, чтобы видеть исходные env-переменные до загрузки `.env`.
- Исключение из `adapter.start()` логируется, после чего выполняется shutdown; при этом код завершения процесса не меняется на ненулевой.
- `_init_services()` при ошибке инициализации приводит к `sys.exit(1)`.
- `logger` модуля создаётся через `logging.getLogger(__name__)`.

## Сохранённая существующая документация
В исходной документации отмечалось, что сохраняемых разделов нет.