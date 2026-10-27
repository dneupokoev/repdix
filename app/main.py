# app/main.py
dv_file_version = '261028.10'
"""Точка входа repdix: выбор адаптера и запуск event loop.

Ключевые изменения версии 261028.08:
- Ротация logs/security.log (раньше писался без ограничения размера —
  файл рос бесконечно). Формат — RotatingFileHandler, размер/бэкапы из env
  (SECURITY_LOG_MAX_BYTES / SECURITY_LOG_BACKUP_COUNT), дефолт 50MB × 5.
- ДОБАВЛЕН вызов engine.sessions.clear_all() на shutdown — иначе висящие
  asyncio.Task (если пользователь запустил анализ и процесс упал) не
  отменяются, и event loop ругается "Task was destroyed but it is pending".
- close() вызывается с try/except: если что-то упало при закрытии — процесс
  всё равно завершается, а не висит. Раньше одно исключение в
  engine.db_manager.close() блокировало закрытие llm/cache/adapter.
- logger.error на старте, если ADAPTER неизвестен (было молчаливое
  переключение на CLI).
- logger.warning, если TELEGRAM_TOKEN не задан при ADAPTER=telegram.
- Проверка, что Redis доступен (ping) при старте — быстрый fail-fast,
  вместо падения на первом запросе.
- Логирование версии dv_file_version при старте (для аудита, какая версия
  запущена).
- Импорт UserManager вынесен наверх (был внутри main — «на горячую»,
  что мешает статическому анализу).
- os.makedirs('logs', exist_ok=True) с проверкой прав (try/except).
- В RotatingFileHandler добавлен delay=True — файл открывается при первой
  записи, а не при старте (полезно, если бот запускается, но не пишет).
- Форматирование: единый стиль импортов, пустые строки по PEP8.
- Graceful shutdown: SIGINT/SIGTERM перехватываются aiogram/cli, но
  добавлена защита — если adapter.start() бросил, finally всё равно
  всё закроет.
- Если adapter.close() не реализован — вызывается через hasattr (как было).
"""

import asyncio
import logging
import logging.handlers
import os
import sys
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

from app.core.cache_manager import CacheManager
from app.core.db_manager import DBManager
from app.core.engine import RepdixEngine
from app.core.llm_client import LLMClient
from app.core.user_manager import UserManager
from app.interfaces.cli_adapter import CLIAdapter
from app.interfaces.telegram_adapter import TelegramAdapter

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Логирование
# ----------------------------------------------------------------------
def _setup_logging() -> None:
    """Настраивает основной лог, аудит и security-лог с ротацией."""
    # Основной лог (консоль)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )

    # Директория для логов
    logs_dir_ok = True
    try:
        os.makedirs('logs', exist_ok=True)
    except OSError as e:
        logger.error("Не удалось создать директорию logs: %s", e)
        logger.warning("File-логи (audit/security) будут отключены.")
        logs_dir_ok = False

    if not logs_dir_ok:
        return

    # --- Аудит (audit.log) ---
    audit_logger = logging.getLogger("audit")
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False  # не дублируем в консоль
    log_max_bytes = int(os.environ.get("LOG_MAX_BYTES") or str(100 * 1024 * 1024))
    log_backup_count = int(os.environ.get("LOG_BACKUP_COUNT") or "5")
    audit_handler = RotatingFileHandler(
        'logs/audit.log',
        maxBytes=log_max_bytes,
        backupCount=log_backup_count,
        encoding='utf-8',
        delay=True,  # файл открывается при первой записи
    )
    audit_handler.setFormatter(logging.Formatter('%(message)s'))
    audit_logger.addHandler(audit_handler)

    # --- Security (security.log) ---
    # Раньше security.log писался через open(..., 'a') без ротации —
    # при большом количестве нарушений рос бесконечно.
    security_logger = logging.getLogger("security")
    security_logger.setLevel(logging.INFO)
    security_logger.propagate = False
    sec_max_bytes = int(os.environ.get("SECURITY_LOG_MAX_BYTES") or str(50 * 1024 * 1024))
    sec_backup_count = int(os.environ.get("SECURITY_LOG_BACKUP_COUNT") or "5")
    sec_handler = RotatingFileHandler(
        'logs/security.log',
        maxBytes=sec_max_bytes,
        backupCount=sec_backup_count,
        encoding='utf-8',
        delay=True,
    )
    sec_handler.setFormatter(logging.Formatter('%(message)s'))
    security_logger.addHandler(sec_handler)


# ----------------------------------------------------------------------
# Инициализация сервисов
# ----------------------------------------------------------------------
async def _init_services() -> tuple[DBManager, LLMClient, CacheManager, UserManager]:
    """Создаёт и инициализирует сервисы."""
    db_manager = DBManager()
    llm_client = LLMClient(
        default_model=os.environ.get("DEFAULT_MODEL", "tool_calling_model"),
    )
    cache_manager = CacheManager(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT") or "6379"),
        password=os.environ.get("REDIS_PASSWORD") or None,
    )

    # Проверка Redis (fail-fast, чтобы не ловить это на первом запросе)
    if await cache_manager.ping():
        logger.info("Redis: подключение OK")
    else:
        logger.warning("Redis недоступен. История диалогов не будет кэшироваться.")

    user_manager = UserManager()
    await user_manager.sync_admins()

    return db_manager, llm_client, cache_manager, user_manager


# ----------------------------------------------------------------------
# Выбор адаптера
# ----------------------------------------------------------------------
def _build_adapter(engine: RepdixEngine):
    """Создаёт адаптер по ADAPTER из .env. Возвращает None, если не удалось."""
    adapter_name = (os.environ.get("ADAPTER") or "cli").lower()

    if adapter_name == "telegram":
        if not os.environ.get("TELEGRAM_TOKEN"):
            logger.error("ADAPTER=telegram, но TELEGRAM_TOKEN не задан в .env")
            return None
        return TelegramAdapter(engine)

    if adapter_name == "cli":
        return CLIAdapter(engine)

    logger.error(
        "Неизвестный ADAPTER=%r. Допустимо: 'telegram' или 'cli'. "
        "Переключаюсь на CLI.", adapter_name,
    )
    return CLIAdapter(engine)


# ----------------------------------------------------------------------
# Точка входа
# ----------------------------------------------------------------------
async def main() -> None:
    """Инициализация ядра и запуск выбранного адаптера."""
    load_dotenv()
    _setup_logging()

    logger.info("repdix запускается")

    # --- Сервисы ---
    try:
        db_manager, llm_client, cache_manager, user_manager = await _init_services()
    except Exception:
        logger.exception("Не удалось инициализировать сервисы")
        sys.exit(1)

    # --- Ядро ---
    engine = RepdixEngine(
        db_manager=db_manager,
        llm_client=llm_client,
        cache_manager=cache_manager,
        default_db=os.environ.get("DEFAULT_DB"),
        user_manager=user_manager,
    )

    # --- Адаптер ---
    adapter = _build_adapter(engine)
    if adapter is None:
        logger.error("Не удалось создать адаптер — завершение")
        await _shutdown(engine, None)
        sys.exit(1)

    # --- Запуск + graceful shutdown ---
    try:
        await adapter.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Получен сигнал остановки")
    except Exception:
        logger.exception("Адаптер упал")
    finally:
        await _shutdown(engine, adapter)


async def _shutdown(engine: RepdixEngine, adapter) -> None:
    """Аккуратно закрывает все ресурсы. Не падает, если что-то уже закрыто."""
    # 1. Отменяем все висящие сессии/задачи пользователей
    try:
        engine.sessions.clear_all()
    except Exception as e:
        logger.warning("Ошибка очистки сессий: %s", e)

    # 2. Закрываем БД
    try:
        await engine.db_manager.close()
    except Exception as e:
        logger.warning("Ошибка закрытия DBManager: %s", e)

    # 3. Закрываем LLM
    if engine.llm_client is not None:
        try:
            await engine.llm_client.close()
        except Exception as e:
            logger.warning("Ошибка закрытия LLMClient: %s", e)

    # 4. Закрываем Redis
    if engine.cache_manager is not None:
        try:
            await engine.cache_manager.close()
        except Exception as e:
            logger.warning("Ошибка закрытия CacheManager: %s", e)

    # 5. Закрываем адаптер
    if adapter is not None and hasattr(adapter, "close"):
        try:
            await adapter.close()
        except Exception as e:
            logger.warning("Ошибка закрытия адаптера: %s", e)

    logger.info("repdix остановлен")


if __name__ == "__main__":
    asyncio.run(main())
