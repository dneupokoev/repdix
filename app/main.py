# app/main.py
dv_file_version = '261027.01'
"""Точка входа repdix: выбор адаптера и запуск event loop."""
import asyncio
import logging
import logging.handlers
from logging.handlers import RotatingFileHandler
import os

from dotenv import load_dotenv

from app.core.cache_manager import CacheManager
from app.core.db_manager import DBManager
from app.core.engine import RepdixEngine
from app.core.llm_client import LLMClient
from app.interfaces.cli_adapter import CLIAdapter
from app.interfaces.telegram_adapter import TelegramAdapter


async def main() -> None:
    """Инициализация ядра и запуск CLI-адаптера."""
    load_dotenv()

    # Настройка основного лога
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    )

    # Настройка аудита в отдельный файл (с ротацией)
    os.makedirs('logs', exist_ok=True)
    audit_logger = logging.getLogger("audit")
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False
    log_max_bytes = int(os.environ.get("LOG_MAX_BYTES") or str(100 * 1024 * 1024))
    log_backup_count = int(os.environ.get("LOG_BACKUP_COUNT") or "5")
    handler = RotatingFileHandler(
        'logs/audit.log',
        maxBytes=log_max_bytes,
        backupCount=log_backup_count,
        encoding='utf-8',
    )
    handler.setFormatter(logging.Formatter('%(message)s'))
    audit_logger.addHandler(handler)

    db_manager = DBManager()
    llm_client = LLMClient(default_model=os.environ.get("DEFAULT_MODEL", "tool_calling_model"))
    cache_manager = CacheManager(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT") or "6379"),
        password=os.environ.get("REDIS_PASSWORD") or None,
    )
    
    # Инициализация менеджера пользователей
    from app.core.user_manager import UserManager
    user_manager = UserManager()
    await user_manager.init_db()
    
    engine = RepdixEngine(
        db_manager=db_manager,
        llm_client=llm_client,
        cache_manager=cache_manager,
        default_db=os.environ.get("DEFAULT_DB"),
        user_manager=user_manager
    )

    adapter_name = os.environ.get("ADAPTER", "cli").lower()

    if adapter_name == "telegram":
        adapter = TelegramAdapter(engine)
    else:
        adapter = CLIAdapter(engine)

    try:
        await adapter.start()
    finally:
        await engine.db_manager.close()
        if engine.llm_client is not None:
            await engine.llm_client.close()
        if engine.cache_manager is not None:
            await engine.cache_manager.close()
        if hasattr(adapter, "close"):
            await adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
