# app/interfaces/cli_adapter.py

Версия файла: `261028.10`.

## Назначение

Консольный адаптер `CLIAdapter` для быстрой отладки ядра `repdix`. Читает пользовательский ввод из `stdin`, передаёт его в `RepdixEngine`, печатает ответы и статусы в `stdout`. Реализует интерфейс `BaseChatInterface` и используется как альтернативный Telegram фронтенд к движку.

Ключевые особенности:
- CLI работает с ролью `admin` или с пользователем, заданным через `CLI_USER_ID`. Если пользователь не админ, RBAC может заблокировать доступ к таблицам, и CLI будет непригоден для отладки SQL.
- Поддерживается `status_callback`: engine присылает промежуточные статусы, CLI их печатает.
- Служебные команды: `:help`, `:clear`, `:quit`, а также `exit` и `:q`.
- `Ctrl+C` во время ожидания ввода завершает CLI; `Ctrl+C` во время выполнения запроса обрабатывается как отмена текущего запроса и не должен ронять цикл.
- При старте печатается баннер с командами, `user_id` и значением `ADAPTER`.
- Ошибки движка печатаются как `⚠️ ...` и не завершают цикл.
- Пустой ввод игнорируется.
- Длинный вывод печатается как есть, без ограничения в 4096 символов, в отличие от Telegram.
- `close()` — graceful no-op для совместимости с `main.py`.

## Класс `CLIAdapter(BaseChatInterface)`

### `__init__(self, engine: RepdixEngine) -> None`

Сохраняет `engine`. Инициализирует:
- `self.user_id = os.environ.get("CLI_USER_ID", "cli")` — идентификатор пользователя CLI, переопределяется через переменную окружения.
- `self.chat_id = self.user_id`.

### `async start(self) -> None`

Основной цикл чтения команд из консоли:
1. Вызывает `_check_cli_role()`.
2. Печатает баннер: название, `ADAPTER`, `CLI_USER_ID`, список служебных команд и подсказку про `Ctrl+C`.
3. В цикле читает строку через `asyncio.to_thread(input, "> ")`.
4. Завершает работу по `EOFError` / `KeyboardInterrupt` в `input()`, а также по командам `exit`, `:quit`, `:q`.
5. Обрабатывает `:help` — печатает справку и продолжает цикл.
6. Обрабатывает `:clear` — перенаправляет запрос в engine как `/clear`.
7. Остальные непустые строки передаёт в `_run_query`.

### `_check_cli_role(self) -> None`

Проверяет через `engine.user_manager.is_admin(self.user_id)`, что пользователь CLI является админом. Если `engine.user_manager` отсутствует, проверка пропускается. Если пользователь не админ, печатает предупреждение в `stderr` о том, что RBAC заблокирует доступ к таблицам, и предлагает добавить пользователя в `ADMIN_USERS` или задать `CLI_USER_ID` с id админа. Ошибки проверки логируются через `logger.warning`.

### `async _run_query(self, text: str) -> None`

Формирует `ChatMessage(user_id, chat_id, text, status_callback)` и передаёт в `engine.process_message(msg)`. Печатает `result.text`.

- `status_callback(status: str)` — печатает статусы, приходящие от engine, например `"🤖 Выполняю execute_sql..."`.
- `asyncio.CancelledError` — печатает `"⛔ Запрос отменён."` и пробрасывает исключение.
- `KeyboardInterrupt` — печатает `"⛔ Запрос отменён (Ctrl+C)."` и продолжает работу.
- Прочие исключения логируются через `logger.exception` и печатаются как `"⚠️ Ошибка: ..."`.

### `_print_help(self) -> None`

Печатает справку по CLI-командам: `:help`, `:clear`, `:quit`, `Ctrl+C`. Уточняет, что остальные команды (`/db`, `/model`, `/sql`, `/history`, `/settings` и т.п.) уходят в engine как обычно.

### `async send_message(self, chat_id: str, text: str) -> None`

Печатает `text` в консоль. Реализация метода `BaseChatInterface`. Аргумент `chat_id` не используется.

### `async send_image(self, chat_id: str, image_path: str) -> None`

Печатает строку `[image] <image_path>`. Реализация метода `BaseChatInterface`.

### `async close(self) -> None`

Graceful shutdown, no-op. Добавлен для совместимости с `main.py`.

## Зависимости

- `asyncio` — `to_thread`, цикл ввода, обработка `CancelledError`.
- `logging` — логгер `logger = logging.getLogger(__name__)`.
- `os` — чтение `CLI_USER_ID` и `ADAPTER` из окружения.
- `sys` — вывод предупреждений в `stderr`.
- `app.core.engine.RepdixEngine` — ядро; используется `process_message(msg)` и, опционально, `engine.user_manager`.
- `app.interfaces.base.BaseChatInterface`, `ChatMessage` — базовый интерфейс и модель сообщения с полями `user_id`, `chat_id`, `text`, `status_callback`.

## Использование в проекте

- `CLIAdapter` реализует `BaseChatInterface` и подключается как один из фронтендов к `RepdixEngine` рядом с Telegram-адаптером.
- По существующей документации используется в `main.py`: вызывается `start()` для запуска цикла, `close()` — при завершении. `close()` является no-op и добавлен для совместимости с `main.py`.
- `engine.process_message(ChatMessage)` — точка входа в ядро; через `status_callback` CLI отображает промежуточные статусы.
- `engine.user_manager.is_admin(user_id)` — проверка роли при старте, связанная с RBAC.

## Переменные окружения

- `CLI_USER_ID` — идентификатор пользователя CLI, по умолчанию `"cli"`. Для полноценной работы должен соответствовать админу (`ADMIN_USERS` в `.env`).
- `ADAPTER` — читается только для информационного вывода в баннере; по умолчанию отображается `"cli"`.

## Ограничения и заметки

- Без роли `admin` RBAC может заблокировать все таблицы, и CLI не сможет выполнять SQL-запросы.
- `:clear` не очищает консоль локально, а отправляет в engine команду `/clear` для очистки истории.
- Вывод CLI не ограничен 4096 символами, поэтому длинные ответы печатаются полностью.
- `Ctrl+C` в `input()` завершает CLI; `Ctrl+C` во время `_run_query()` обрабатывается как отмена текущего запроса и не завершает цикл.
- `asyncio.CancelledError` в `_run_query()` печатается и пробрасывается дальше.