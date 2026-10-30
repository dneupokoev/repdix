"""Тесты аудита ручного SQL (/sql) в logs/manual_sql.log.

Проверяем, что ручной SQL пишется в отдельный файл целиком (без обрезки),
а при ошибке выполнения в записи появляется текст ошибки.

Логгер "manual_sql" инициализируется один раз (RotatingFileHandler на
logs/manual_sql.log), поэтому в тестах его путь подменяется на tmp_path:
реальный файл аудита не засоряется.
"""

from __future__ import annotations

dv_file_version = '261030.01'

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import manual_sql_audit as msa
from app.core.engine import RepdixEngine
from app.core.manual_sql_audit import log_manual_sql
from app.interfaces.base import ChatMessage


def _reset_audit_logger(logger: logging.Logger, monkeypatch) -> None:
    """Снимает хэндлеры и сбрасывает флаг инициализации логгера аудита."""
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001 — файл мог не открыться
            pass
    monkeypatch.setattr(logger, msa._INIT_FLAG, False, raising=False)


@pytest.fixture
def manual_sql_log(tmp_path, monkeypatch) -> Path:
    """Перенаправляет аудит /sql в tmp_path на время теста."""
    log_path = tmp_path / "manual_sql.log"
    monkeypatch.setattr(msa, "_LOG_PATH", str(log_path))

    logger = logging.getLogger(msa._LOGGER_NAME)
    _reset_audit_logger(logger, monkeypatch)
    try:
        yield log_path
    finally:
        _reset_audit_logger(logger, monkeypatch)


def _flush() -> None:
    """Сбрасывает буферы хэндлеров, чтобы файл был актуален на диске."""
    for handler in logging.getLogger(msa._LOGGER_NAME).handlers:
        try:
            handler.flush()
        except Exception:  # noqa: BLE001 — файл мог не открыться
            pass


def _read_entries(path: Path) -> list[dict]:
    """Читает JSON-записи аудита (formatter может добавить префикс)."""
    if not path.exists():
        return []
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        start = line.find('{"timestamp"')
        if start < 0:
            continue
        try:
            data, _end = json.JSONDecoder().raw_decode(line[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            entries.append(data)
    return entries


def _find_entry(path: Path, marker: str) -> dict:
    """Последняя запись, в SQL которой есть маркер (уникальный номер)."""
    entries = [
        e for e in _read_entries(path) if marker in str(e.get("sql", ""))
    ]
    assert entries, f"В {path} нет записи аудита с маркером {marker!r}"
    return entries[-1]


_LONG_SQL = (
    "SELECT id FROM main_db.orders WHERE id > 987654 AND "
    + " AND ".join(f"flag_{i} = {i}" for i in range(40))
    + " LIMIT 5"
)


def _fake_db(
    rows: list[dict] | None = None,
    error: Exception | None = None,
) -> MagicMock:
    """Заглушка DBManager: синхронные методы — MagicMock, async — AsyncMock.

    engine вызывает синхронные get_db_config/get_db_type/get_db_names,
    поэтому целиком AsyncMock использовать нельзя.
    """
    db = MagicMock()
    db.get_db_config.return_value = {"type": "mysql", "database": "main_db"}
    db.get_db_type.return_value = "mysql"
    db.get_db_names.return_value = ["main_db"]
    db.get_db_display_name.side_effect = lambda name: name
    db.get_table_schema = AsyncMock(return_value=[])
    if error is not None:
        db.execute_query = AsyncMock(side_effect=error)
    else:
        db.execute_query = AsyncMock(return_value=rows or [])
    return db


def _make_engine(db_manager) -> RepdixEngine:
    """Движок без LLM/кэша: /sql обрабатывается детерминированно."""
    return RepdixEngine(
        db_manager=db_manager,
        llm_client=None,
        cache_manager=None,
        default_db="main_db",
        user_manager=None,
    )


def _run_sql(engine: RepdixEngine, sql: str, user_id: str) -> str:
    """Выполняет /sql и возвращает текст ответа.

    Состояние сессии настраивается ВНУТРИ корутины: SessionState/asyncio.Lock
    должны создаваться уже в рабочем event loop.
    """

    async def _call() -> str:
        engine.sessions.get(user_id).current_db = "main_db"
        response = await engine.process_message(
            ChatMessage(user_id=user_id, chat_id="c-audit", text=f"/sql {sql}")
        )
        return response.text

    return asyncio.run(_call())


def test_log_manual_sql_writes_full_sql_and_duration(
    manual_sql_log: Path,
) -> None:
    """log_manual_sql пишет полный SQL (без обрезки) и duration_ms."""
    log_manual_sql(
        user_id="u-audit",
        db_name="main_db",
        sql=_LONG_SQL,
        duration_ms=42.5,
        rows_returned=3,
    )
    _flush()

    entry = _find_entry(manual_sql_log, "987654")
    # Главное требование: SQL сохранён целиком, обрезки нет.
    assert entry["sql"] == _LONG_SQL
    assert "LIMIT 5" in entry["sql"]
    assert entry["type"] == "manual_sql"
    assert entry["user_id"] == "u-audit"
    assert entry["db_name"] == "main_db"
    assert entry["duration_ms"] == pytest.approx(42.5)
    assert entry["status"] == "success"
    assert entry["rows_returned"] == 3
    assert "error" not in entry


def test_log_manual_sql_writes_error(manual_sql_log: Path) -> None:
    """При ошибке выполнения в запись аудита попадает текст ошибки."""
    sql = "SELECT id FROM main_db.orders WHERE id > 987655 LIMIT 5"
    log_manual_sql(
        user_id="u-audit",
        db_name="main_db",
        sql=sql,
        duration_ms=7.0,
        error="boom: lost connection",
    )
    _flush()

    entry = _find_entry(manual_sql_log, "987655")
    assert entry["sql"] == sql
    assert entry["status"] == "error"
    assert entry["error"] == "boom: lost connection"
    assert entry["duration_ms"] == pytest.approx(7.0)


def test_sql_command_writes_to_manual_sql_log(manual_sql_log: Path) -> None:
    """Команда /sql (успех) оставляет запись в logs/manual_sql.log."""
    sql = (
        "SELECT id FROM main_db.orders "
        "WHERE id > 987656 AND status = 'new' LIMIT 5"
    )
    engine = _make_engine(_fake_db(rows=[{"id": 1}, {"id": 2}]))

    _run_sql(engine, sql, user_id="u-sql-ok")
    _flush()

    entry = _find_entry(manual_sql_log, "987656")
    assert entry["sql"] == sql
    assert entry["status"] == "success"
    assert entry["rows_returned"] == 2
    assert isinstance(entry["duration_ms"], (int, float))


def test_sql_command_error_writes_error_to_manual_sql_log(
    manual_sql_log: Path,
) -> None:
    """/sql с ошибкой выполнения пишет error в logs/manual_sql.log."""
    sql = "SELECT id FROM main_db.orders WHERE id > 987657 LIMIT 5"
    engine = _make_engine(_fake_db(error=RuntimeError("boom 987657")))

    text = _run_sql(engine, sql, user_id="u-sql-err")
    _flush()

    assert "Ошибка выполнения запроса" in text

    entry = _find_entry(manual_sql_log, "987657")
    assert entry["sql"] == sql
    assert entry["status"] == "error"
    assert entry["error"] == "boom 987657"
