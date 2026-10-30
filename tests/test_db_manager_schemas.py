"""Тесты multi-schema поддержки DBManager (без реального подключения к БД)."""

import asyncio
import logging
from unittest.mock import AsyncMock

from app.core.db_manager import DBManager


def make_manager(databases: dict) -> DBManager:
    """Создаёт DBManager с подменённым конфигом (файл не читается)."""
    mgr = DBManager(config_path="__nonexistent_config__.yaml")
    mgr.config = {"databases": databases}
    return mgr


def run(coro):
    """Прогоняет корутину в отдельном event loop (без pytest-asyncio)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# get_schemas
# ---------------------------------------------------------------------------
def test_get_schemas_explicit():
    mgr = make_manager({
        "db": {"type": "mysql", "database": "db", "schemas": ["db_a", "db_b"]},
    })
    assert run(mgr.get_schemas("db")) == ["db_a", "db_b"]


def test_get_schemas_fallback():
    mgr = make_manager({"db": {"type": "mysql", "database": "main_db"}})
    assert run(mgr.get_schemas("db")) == ["main_db"]


def test_get_schemas_excludes_technical():
    mgr = make_manager({
        "db": {
            "type": "mysql",
            "database": "db",
            "schemas": ["db_a", "technical_archive"],
        },
    })
    assert run(mgr.get_schemas("db")) == ["db_a"]


def test_get_schemas_auto():
    mgr = make_manager({
        "db": {
            "type": "mysql",
            "database": "db",
            "schemas": "auto",
            "exclude_schemas": ["mysql", "information_schema"],
        },
    })
    mgr.execute_query = AsyncMock(return_value=[
        {"schema_name": "db_a"},
        {"schema_name": "db_b"},
        {"schema_name": "mysql"},
        {"schema_name": "information_schema"},
    ])
    assert run(mgr.get_schemas("db")) == ["db_a", "db_b"]


def test_get_schemas_auto_failure_returns_empty(tmp_path, caplog):
    cfg = tmp_path / "databases.yaml"
    cfg.write_text(
        "databases:\n"
        "  db:\n"
        "    type: mysql\n"
        "    database: db\n"
        "    schemas: auto\n"
        "    exclude_schemas: [mysql, information_schema]\n",
        encoding="utf-8",
    )
    mgr = DBManager(config_path=str(cfg))
    mgr.execute_query = AsyncMock(side_effect=RuntimeError("boom"))

    with caplog.at_level(logging.ERROR):
        assert run(mgr.get_schemas("db")) == []
        # Повторный вызов не должен отдавать закэшированный пустой список.
        assert run(mgr.get_schemas("db")) == []

    assert "возвращаю пустой список" in caplog.text
    assert mgr.execute_query.await_count == 2


# ---------------------------------------------------------------------------
# get_table_list
# ---------------------------------------------------------------------------
def test_get_table_list_fully_qualified():
    mgr = make_manager({
        "db": {"type": "mysql", "database": "db", "schemas": ["db_a", "db_b"]},
    })
    mgr.get_schemas = AsyncMock(return_value=["db_a", "db_b"])
    mgr.execute_query = AsyncMock(side_effect=[
        [{"table_name": "users"}],
        [{"table_name": "orders"}],
    ])
    assert run(mgr.get_table_list("db")) == ["db_a.users", "db_b.orders"]


# ---------------------------------------------------------------------------
# Кэш
# ---------------------------------------------------------------------------
def test_cache_invalidation():
    mgr = make_manager({})
    mgr._cache_set(("schemas", "db"), ["db_a"])
    mgr._cache_set(("tables", "db", "*"), ["db_a.users"])
    assert mgr._cache_get(("schemas", "db")) == ["db_a"]

    mgr.invalidate_schema_cache("db")
    assert mgr._schema_cache == {}

    mgr._cache_set(("schemas", "db1"), ["db_a"])
    mgr.invalidate_schema_cache()
    assert mgr._schema_cache == {}


# ---------------------------------------------------------------------------
# Описание БД
# ---------------------------------------------------------------------------
def test_get_db_display_name_with_description():
    mgr = make_manager({
        "main_db": {
            "type": "mysql",
            "database": "main_db",
            "description": "Основная БД",
        },
    })
    assert mgr.get_db_display_name("main_db") == "main_db — Основная БД"


def test_get_db_display_name_without_description():
    mgr = make_manager({"main_db": {"type": "mysql", "database": "main_db"}})
    assert mgr.get_db_display_name("main_db") == "main_db"


def test_get_db_description_empty():
    mgr = make_manager({
        "main_db": {"type": "mysql", "database": "main_db", "description": ""},
    })
    assert mgr.get_db_description("main_db") == ""
