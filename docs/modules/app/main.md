# app/main.py

## Назначение
`app/main.py` — точка входа приложения **repdix**. Модуль:
- загружает переменные окружения из `.env`;
- настраивает консольное логирование и файловые логи `audit.log` / `security.log` с ротацией;
- инициализирует `DBManager`, `LLMClient`, `CacheManager`, `UserManager`;
- создаёт `RepdixEngine`;
- выбирает и запускает адаптер `cli` или `telegram`;
- выполняет graceful shutdown при завершении или ошибке.

Запуск предусмотрен как скрипт: `asyncio.run(main())`.

## Версия
`dv_file_version = '261028.10'`.  
В docstring описаны изменения версии `261028.08`, но фактическая переменная версии — `261028.10`. В приведённом коде версия при старте не логируется, хотя это заявлено в docstring.

## Основные функции

- `_setup_logging() -> None`
  - Настраивает `logging.basicConfig` уровня `INFO`.
  - Создаёт директорию `logs` через `os.makedirs(..., exist_ok=True)`.
  - При ошибке создания директории пишет ошибку и отключает файловые логи.
  - Настраивает логгер `audit` с `RotatingFileHandler` для `logs/audit.log`.
  - Настраивает логгер `security` с `RotatingFileHandler` для `logs/security.log`.
  - Для файловых логов использует `delay=True`, `encoding='utf-8'`, формат `%(message)s`, `propagate=False`.
  - Размеры ротации берёт из env:
    - `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT` — audit, по умолчанию 100 MB × 5;
    - `SECURITY_LOG_MAX_BYTES`, `SECURITY_LOG_BACKUP_COUNT` — security, по умолчанию 50 MB × 5.

- `async _init_services() -> tuple[DBManager, LLMClient, CacheManager, UserManager]`
  - Создаёт `DBManager()`.
  - Создаёт `LLMClient(default_model=os.environ.get("DEFAULT_MODEL", "tool_calling_model"))`.
  - Создаёт `CacheManager(host=..., port=..., password=...)` по env `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`.
  - Вызывает `await cache_manager.ping()`.
    - При успехе логирует `Redis: подключение OK`.
    - При неудаче пишет warning и продолжает запуск.
  - Создаёт `UserManager()` и вызывает `await user_manager.sync_admins()`.
  - Возвращает кортеж инициализированных сервисов.

- `_build_adapter(engine: RepdixEngine)`
  - Читает `ADAPTER`, по умолчанию `cli`.
  - Для `telegram` проверяет наличие `TELEGRAM_TOKEN`; если токена нет — логирует ошибку и возвращает `None`.
  - Для `telegram` возвращает `TelegramAdapter(engine)`.
  - Для `cli` возвращает `CLIAdapter(engine)`.
  - Для неизвестного значения логирует ошибку и возвращает `CLIAdapter(engine)` как fallback.

- `async main() -> None`
  - Вызывает `load_dotenv()` и `_setup_logging()`.
  - Логирует `repdix запускается`.
  - Инициализирует сервисы через `_init_services()`; при исключении логирует его и завершает процесс с `sys.exit(1)`.
  - Создаёт `RepdixEngine(db_manager=..., llm_client=..., cache_manager=..., default_db=os.environ.get("DEFAULT_DB"), user_manager=...)`.
  - Создаёт адаптер через `_build_adapter(engine)`.
  - Если адаптер не создан — логирует ошибку, вызывает `_shutdown(engine, None)` и завершает процесс с `sys.exit(1)`.
  - Запускает `await adapter.start()`.
  - Перехватывает `KeyboardInterrupt` / `asyncio.CancelledError` и другие исключения.
  - В `finally` всегда вызывает `_shutdown(engine, adapter)`.

- `async _shutdown(engine: RepdixEngine, adapter) -> None`
  - Вызывает `engine.sessions.clear_all()` без `await`; ошибки логирует warning.
  - Закрывает `engine.db_manager` через `await close()`.
  - Закрывает `engine.llm_client`, если он задан.
  - Закрывает `engine.cache_manager`, если он задан.
  - Закрывает адаптер через `await adapter.close()`, если у адаптера есть `close`.
  - Все ошибки закрытия перехватываются и логируются warning, чтобы shutdown не падал.
  - Логирует `repdix остановлен`.

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
- `python app/main.py`
- или `python -m app.main`

Другие модули используются внутри `main.py` через создание объектов и вызовы:
- `CacheManager.ping()`, `CacheManager.close()`
- `UserManager.sync_admins()`
- `DBManager.close()`
- `LLMClient.close()`
- `RepdixEngine(...)`, `engine.sessions.clear_all()`
- `adapter.start()`, `adapter.close()`

## Поведение и ограничения
- При неизвестном `ADAPTER` приложение не завершается, а переключается на `CLIAdapter` после `logger.error`.
- При `ADAPTER=telegram` без `TELEGRAM_TOKEN` адаптер не создаётся, выполняется shutdown и процесс завершается с кодом `1`.
- Проверка Redis в docstring названа fail-fast, но фактически при неудачном `ping()` приложение только пишет warning и продолжает запуск.
- Числовые env-переменные приводятся через `int(...)`; при некорректном значении будет исключение.
- Если директорию `logs` создать не удалось, файловые audit/security-логи отключаются, консольное логирование остаётся.
- Shutdown устойчив к ошибкам: исключения при закрытии ресурсов логируются, но не прерывают завершение.
- Собственных классов модуль не определяет; все классы импортируются из других модулей.

## Сохранённая существующая документация
Существующая документация в предоставленном материале отсутствует, поэтому сохраняемых разделов нет.