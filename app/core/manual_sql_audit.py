dv_file_version = '261030.06'
"""Отдельный аудит ручных SQL-запросов (команда /sql).

Зачем отдельный файл:
    /sql — осознанный ручной доступ к БД, и его вызовы нужно хранить
    отдельно от audit.log (там весь диалог) и security.log (там отказы).
    При этом ограничения сложности SQL к /sql НЕ применяются (решение
    владельца проекта): модуль только журналирует, ничего не блокирует.

Ротация и формат:
    RotatingFileHandler на logs/manual_sql.log с maxBytes=LOG_MAX_BYTES
    (по умолчанию 104857600) и backupCount=LOG_BACKUP_COUNT (по умолчанию
    5) — те же env-переменные, что у audit.log/security.log.
    Formatter берётся у логгера "audit" (fallback — root, затем
    стандартный), чтобы не дублировать настройки форматирования.

Устойчивость:
    Если файл лога открыть не удалось (права, отсутствие диска), пишем
    WARNING и продолжаем работать без аудита — /sql не должен падать
    из-за проблем с логированием.
"""

import json
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler

__all__ = ["get_manual_sql_logger", "log_manual_sql"]

_LOGGER_NAME = "manual_sql"
_LOG_PATH = os.path.join("logs", "manual_sql.log")
_INIT_FLAG = "_manual_sql_audit_initialized"

_FALLBACK_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    """Читает целое из env с безопасным fallback (не роняет приложение)."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(minimum, int(str(raw).strip()))
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning(
            "Некорректное значение %s=%r, использую %d", name, raw, default,
        )
        return default


def _inherit_formatter() -> logging.Formatter:
    """Возвращает formatter audit.log (или root), чтобы формат был единым."""
    for logger_name in ("audit", ""):
        for handler in logging.getLogger(logger_name).handlers:
            if handler.formatter is not None:
                return handler.formatter
    return logging.Formatter(_FALLBACK_FORMAT)


def get_manual_sql_logger() -> logging.Logger:
    """Возвращает логгер manual_sql, инициализированный ровно один раз."""
    logger = logging.getLogger(_LOGGER_NAME)

    if getattr(logger, _INIT_FLAG, False):
        return logger

    # Не дублируем handler, если его уже добавили (тесты, повторный импорт).
    if any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        setattr(logger, _INIT_FLAG, True)
        return logger

    max_bytes = _env_int("LOG_MAX_BYTES", 104857600, minimum=1)
    backup_count = _env_int("LOG_BACKUP_COUNT", 5, minimum=0)

    try:
        directory = os.path.dirname(_LOG_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        handler = RotatingFileHandler(
            _LOG_PATH,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    except OSError as e:
        # Graceful degradation: /sql должен работать даже без аудита.
        logging.getLogger(__name__).warning(
            "Не удалось открыть %s для аудита /sql: %s", _LOG_PATH, e,
        )
        setattr(logger, _INIT_FLAG, True)
        return logger

    handler.setFormatter(_inherit_formatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    # Запись не должна уходить в общий лог: у /sql свой файл.
    logger.propagate = False
    setattr(logger, _INIT_FLAG, True)
    return logger


def log_manual_sql(
    user_id: str,
    db_name: str | None,
    sql: str,
    duration_ms: float,
    rows_returned: int | None = None,
    error: str | None = None,
) -> None:
    """Пишет одну запись аудита ручного SQL в logs/manual_sql.log.

    Args:
        user_id: ID пользователя, выполнившего /sql.
        db_name: Имя текущей БД.
        sql: ПОЛНЫЙ текст запроса (не обрезается — это аудит).
        duration_ms: Длительность выполнения в миллисекундах.
        rows_returned: Число строк (только при успехе).
        error: Текст ошибки (None при успехе).

    Никогда не выбрасывает исключение: сбой аудита не должен ломать /sql.
    """
    payload = {
        "timestamp": datetime.now().isoformat(),
        "type": "manual_sql",
        "user_id": str(user_id or ""),
        "db_name": db_name,
        "sql": sql if isinstance(sql, str) else str(sql),
        "duration_ms": round(float(duration_ms or 0.0), 3),
        "status": "error" if error is not None else "success",
    }
    if error is not None:
        payload["error"] = str(error)
    else:
        payload["rows_returned"] = int(rows_returned or 0)

    try:
        get_manual_sql_logger().info(json.dumps(payload, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 — аудит не должен ронять бота
        logging.getLogger(__name__).warning(
            "Не удалось записать аудит /sql для %s: %s", user_id, e,
        )
