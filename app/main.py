# app/main.py
dv_file_version = '261030.03'
"""Точка входа repdix: выбор адаптера и запуск event loop"""

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
def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """Читает целое из env с fallback. Не роняет приложение."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(minimum, int(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning(
            "Некорректное значение %s=%r, использую %d", name, raw, default,
        )
        return default


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
    log_max_bytes = _env_int("LOG_MAX_BYTES", 100 * 1024 * 1024)
    log_backup_count = _env_int("LOG_BACKUP_COUNT", 5, minimum=0)
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
    sec_max_bytes = _env_int("SECURITY_LOG_MAX_BYTES", 50 * 1024 * 1024)
    sec_backup_count = _env_int("SECURITY_LOG_BACKUP_COUNT", 5, minimum=0)
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
    raw_adapter = os.environ.get("ADAPTER")
    adapter_name = (raw_adapter or "cli").lower()
    logger.info(
        "Выбор адаптера: ADAPTER=%r (effective=%r). "
        "TELEGRAM_TOKEN задан: %s. CLI_USER_ID: %r. "
        "Если это не то, что вы ожидали — pipenv run перезаписывает "
        "env-переменные из .env; для CLI используйте прямой запуск: "
        ".venv/bin/python -m app.main",
        raw_adapter,
        adapter_name,
        bool(os.environ.get("TELEGRAM_TOKEN")),
        os.environ.get("CLI_USER_ID"),
    )

    if adapter_name == "telegram":
        if os.environ.get("CLI_USER_ID"):
            logger.warning(
                "ADAPTER=telegram, но CLI_USER_ID задан (%r). "
                "Возможно, вы хотели CLI: pipenv run загружает .env "
                "и перезаписывает env-переменные из командной строки. "
                "Для CLI используйте: env ADAPTER=cli ... "
                ".venv/bin/python -m app.main",
                os.environ.get("CLI_USER_ID"),
            )
        if not os.environ.get("TELEGRAM_TOKEN"):
            logger.error(
                "ADAPTER=telegram, но TELEGRAM_TOKEN не задан в .env"
            )
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
    # override=False — документированное поведение python-dotenv:
    # явные env-переменные НЕ перезаписываются.
    # load_dotenv ДО _setup_logging: иначе LOG_MAX_BYTES/SECURITY_LOG_*
    # из .env не применяются (молча берутся дефолты).
    load_dotenv(override=False)
    _setup_logging()
    logger.info(
        "DEBUG main: ADAPTER после load_dotenv=%r",
        os.environ.get("ADAPTER"),
    )
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
    # Фоновая очистка TTL сессий (иначе при малом числе пользователей
    # просроченные сессии живут до рестарта).
    engine.sessions.start_cleanup_task()

    # --- Адаптер ---
    adapter = _build_adapter(engine)
    logger.info(
        "Адаптер создан: %s",
        type(adapter).__name__ if adapter is not None else None,
    )
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
    # 0. Останавливаем фоновую очистку (до clear_all), чтобы таск не
    #    продолжал работать над уже освобождённым хранилищем.
    try:
        await engine.sessions.stop_cleanup_task()
    except Exception as e:
        logger.warning("Ошибка остановки cleanup-таска сессий: %s", e)

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
