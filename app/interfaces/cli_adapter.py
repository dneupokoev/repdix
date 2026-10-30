# app/interfaces/cli_adapter.py
dv_file_version = '261028.10'
"""Консольный адаптер для быстрой отладки ядра repdix.

Ключевые изменения версии 261028.10:
- ВАЖНО: CLI теперь работает с ролью admin (или настраиваемой через
  CLI_USER_ID / CLI_ROLE). Раньше user_id="cli" → UserManager.get_user_roles
  возвращал [] → RBAC блокировал все запросы, и CLI был бесполезен для
  отладки (нельзя было выполнить ни одного SQL).
- Добавлен status_callback: engine присылает статусы ("🤖 Выполняю
  execute_sql..."), CLI их печатает. Раньше статусы молча терялись.
- Добавлены служебные команды: `:help`, `:clear`, `:quit` (плюс `exit`).
- Ctrl+C во время выполнения запроса НЕ завершает процесс, а отменяет
  текущий запрос (аналог /stop). Раньше KeyboardInterrupt ловился только
  в input(), а во время обработки — валил всё.
- При старте печатается подсказка (команды, user_id, роль).
- Ошибки движка печатаются как `⚠️ ...` и не роняют цикл.
- Пустой ввод игнорируется.
- Вывод результата: если текст длинный — печатается как есть (CLI не
  ограничен 4096 символами, в отличие от Telegram).
- close() — graceful no-op (для совместимости с main.py).
"""

import asyncio
import logging
import os
import sys

from app.core.engine import RepdixEngine
from app.interfaces.base import BaseChatInterface, ChatMessage

logger = logging.getLogger(__name__)


class CLIAdapter(BaseChatInterface):
    """Адаптер CLI: читает ввод из stdin, печатает ответы в stdout."""

    def __init__(self, engine: RepdixEngine) -> None:
        self.engine = engine
        # user_id для CLI: по умолчанию "cli", можно переопределить через env.
        # ВАЖНО: если user_id не админ — RBAC заблокирует все таблицы,
        # и CLI будет бесполезен. См. start().
        self.user_id = os.environ.get("CLI_USER_ID", "cli")
        self.chat_id = self.user_id

    # ------------------------------------------------------------------
    # Старт
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Цикл чтения команд из консоли."""
        # Проверка ролей — предупреждаем, если CLI не админ
        self._check_cli_role()

        print("=" * 60)
        print("repdix CLI")
        print(f"ADAPTER: {os.environ.get('ADAPTER', 'cli')!r}")
        print(f"CLI_USER_ID: {self.user_id}")
        print("Команды: :help, :clear, :quit (или exit). "
              "Ctrl+C — отмена запроса.")
        print("=" * 60)

        while True:
            try:
                user_input = await asyncio.to_thread(input, "> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            # Служебные команды CLI
            cmd = user_input.lower()
            if cmd in ("exit", ":quit", ":q"):
                break
            if cmd == ":help":
                self._print_help()
                continue
            if cmd == ":clear":
                # Передаём в engine как /clear — он очистит историю
                user_input = "/clear"

            await self._run_query(user_input)

        print("CLI завершён.")

    # ------------------------------------------------------------------
    # Проверка ролей
    # ------------------------------------------------------------------
    def _check_cli_role(self) -> None:
        """Предупреждает, если у CLI-пользователя нет роли admin.

        Без admin RBAC заблокирует все таблицы, и CLI не сможет
        выполнять запросы (кроме служебных команд).
        """
        um = getattr(self.engine, "user_manager", None)
        if um is None:
            return
        try:
            if not um.is_admin(self.user_id):
                print(
                    f"⚠️  ВНИМАНИЕ: user_id={self.user_id!r} не является админом.\n"
                    f"   RBAC заблокирует доступ к таблицам.\n"
                    f"   Добавьте {self.user_id} в ADMIN_USERS (.env) или\n"
                    f"   задайте CLI_USER_ID=<id админа> в .env.",
                    file=sys.stderr,
                )
        except Exception as e:
            logger.warning("Не удалось проверить роль CLI: %s", e)

    # ------------------------------------------------------------------
    # Выполнение запроса
    # ------------------------------------------------------------------
    async def _run_query(self, text: str) -> None:
        """Отправляет запрос в engine и печатает ответ.

        Ctrl+C во время выполнения отменяет текущий запрос (не завершает CLI).
        """
        async def status_callback(status: str) -> None:
            """Печатает статус от engine (например, '🤖 Выполняю execute_sql...')."""
            print(f"  {status}")

        msg = ChatMessage(
            user_id=self.user_id,
            chat_id=self.chat_id,
            text=text,
            status_callback=status_callback,
        )

        try:
            result = await self.engine.process_message(msg)
            print(result.text)
        except asyncio.CancelledError:
            print("⛔ Запрос отменён.")
            raise
        except KeyboardInterrupt:
            print("\n⛔ Запрос отменён (Ctrl+C).")
        except Exception as e:
            logger.exception("Ошибка CLI-запроса")
            print(f"⚠️ Ошибка: {e}")

    # ------------------------------------------------------------------
    # Помощь
    # ------------------------------------------------------------------
    def _print_help(self) -> None:
        print(
            "CLI-команды:\n"
            "  :help   — эта справка\n"
            "  :clear  — очистить историю (то же, что /clear)\n"
            "  :quit   — выход (или exit)\n"
            "  Ctrl+C  — отменить текущий запрос\n"
            "Все остальные команды (/db, /model, /sql, /history, /settings и т.п.) —\n"
            "уходят в engine как обычно."
        )

    # ------------------------------------------------------------------
    # Интерфейс BaseChatInterface
    # ------------------------------------------------------------------
    async def send_message(self, chat_id: str, text: str) -> None:
        """Вывод текстового сообщения в консоль."""
        print(text)

    async def send_image(self, chat_id: str, image_path: str) -> None:
        """Вывод пути к изображению в консоль."""
        print(f"[image] {image_path}")

    async def close(self) -> None:
        """Graceful shutdown для CLI (no-op)."""
        return
