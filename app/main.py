# app/main.py
dv_file_version = '261026.04'
"""Точка входа repdix: выбор адаптера и запуск event loop."""
import asyncio
import logging
import logging.handlers
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

    # Настройка аудита в отдельный файл
    os.makedirs('logs', exist_ok=True)
    audit_logger = logging.getLogger("audit")
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False
    handler = logging.FileHandler('logs/audit.log', encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'))
    audit_logger.addHandler(handler)

    db_manager = DBManager()
    llm_client = LLMClient()
    cache_manager = CacheManager(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", 6379))
    )
    engine = RepdixEngine(
        db_manager=db_manager,
        llm_client=llm_client,
        cache_manager=cache_manager,
        default_db=os.environ.get("DEFAULT_DB")
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
