# app/interfaces/cli_adapter.py

## Назначение

Консольный адаптер (`CLIAdapter`) для быстрой отладки ядра `repdix`. Читает пользовательский ввод из `stdin`, передаёт его в `RepdixEngine`, печатает ответы и статусы в `stdout`. Является реализацией интерфейса `BaseChatInterface` и используется как альтернативный (не-Telegram) фронтенд к движку.

Версия файла: `261028.10`.

Ключевые особенности:
- CLI работает с ролью `admin` (или настраиваемой через `CLI_USER_ID`). Без admin RBAC блокирует все таблицы, и CLI непригоден для отладки SQL.
- Поддерживается `status_callback` — engine присылает статусы (например, `"🤖 Выполняю execute_sql..."`), CLI их печатает.
- Служебные команды: `:help`, `:clear`, `:quit` (плюс `exit`, `:q`).
- `Ctrl+C` во время выполнения запроса НЕ завершает процесс, а отменяет текущий запрос.
- При старте печатается подсказка (команды, `user_id`, роль).
- Ошибки движка печатаются как `⚠️ ...` и не роняют цикл.
- Пустой ввод игнорируется.
- Длинный вывод печатается как есть (без ограничения в 4096 символов, в отличие от Telegram).
- `close()` — graceful no-op для совместимости с `main.py`.

## Класс `CLIAdapter(BaseChatInterface)`

### `__init__(self, engine: RepdixEngine) -> None`
Сохраняет `engine`. Инициализирует:
- `self.user_id = os.environ.get("CLI_USER_ID", "cli")` — идентификатор пользователя CLI (переопределяется через env).
- `self.chat_id = self.user_id`.

### `async start(self) -> None`
Основной цикл чтения команд из консоли:
1. Вызывает `_check_cli_role()`.
2. Печатает баннер с `user_id` и списком команд.
3. В цикле читает строку через `asyncio.to_thread(input, "> ")`.
4. Завершает работу по `EOFError` / `KeyboardInterrupt` в `input()`, а также по `exit`, `:quit`, `:q`.
5. Обрабатывает `:help` (печать справки) и `:clear` (перенаправляет в engine как `/clear`).
6. Остальные непустые строки передаёт в `_run_query`.

### `_check_cli_role(self) -> None`
Проверяет через `engine.user_manager.is_admin(self.user_id)`, что пользователь CLI является админом. Если нет — печатает предупреждение в `stderr` о том, что RBAC заблокирует доступ к таблицам. Ошибки проверки логируются через `logger.warning`.

### `async _run_query(self, text: str) -> None`
Формирует `ChatMessage(user_id, chat_id, text, status_callback)` и передаёт в `engine.process_message(msg)`. Печатает `result.text`.
- `status_callback(status)` — печатает статусы, приходящие от engine.
- `asyncio.CancelledError` — печатает `"⛔ Запрос отменён."` и пробрасывает исключение.
- `KeyboardInterrupt` — печатает `"⛔ Запрос отменён (Ctrl+C)."` и продолжает цикл.
- Прочие исключения логируются (`logger.exception`) и печатаются как `"⚠️ Ошибка: ..."`.

### `_print_help(self) -> None`
Печатает справку по CLI-командам: `:help`, `:clear`, `:quit`, `Ctrl+C`. Уточняет, что остальные команды (`/db`, `/model`, `/sql`, `/history`, `/settings` и т.п.) уходят в engine как обычно.

### `async send_message(self, chat_id: str, text: str) -> None`
Печатает `text` в консоль. Реализация `BaseChatInterface`.

### `async send_image(self, chat_id: str, image_path: str) -> None`
Печатает строку `[image] <image_path>`. Реализация `BaseChatInterface`.

### `async close(self) -> None`
Graceful shutdown, no-op (для совместимости с `main.py`).

## Зависимости

- `asyncio` — `to_thread`, цикл ввода, обработка `CancelledError`.
- `logging` — логгер `logger = logging.getLogger(__name__)`.
- `os` — чтение `CLI_USER_ID` из окружения.
- `sys` — вывод предупреждений в `stderr`.
- `app.core.engine.RepdixEngine` — ядро; используется `process_message(msg)` и, опционально, `engine.user_manager`.
- `app.interfaces.base.BaseChatInterface`, `ChatMessage` — базовый интерфейс и модель сообщения (`user_id`, `chat_id`, `text`, `status_callback`).

## Использование в проекте

- `CLIAdapter` реализует `BaseChatInterface` и подключается как один из фронтендов к `RepdixEngine` (рядом с Telegram-адаптером).
- Используется в `main.py`: вызывается `start()` для запуска цикла, `close()` — при завершении (`no-op`, добавлен именно для совместимости с `main.py`).
- `engine.process_message(ChatMessage)` — точка входа в ядро; через `status_callback` CLI отображает промежуточные статусы.
- `engine.user_manager.is_admin(user_id)` — проверка роли при старте (RBAC).

## Переменные окружения

- `CLI_USER_ID` — идентификатор пользователя CLI (по умолчанию `"cli"`). Для полноценной работы должен соответствовать админу (`ADMIN_USERS` в `.env`).