"""Тесты RepdixEngine._extract_table_from_sql (парсинг через sqlglot)."""

import pytest

from app.core.engine import RepdixEngine


@pytest.fixture
def engine():
    """Движок без зависимостей: все аргументы __init__ опциональны."""
    return RepdixEngine()


def test_simple_table(engine):
    assert engine._extract_table_from_sql("SELECT * FROM t") == (None, "t")


def test_qualified_table(engine):
    assert engine._extract_table_from_sql("SELECT * FROM db.t") == ("db", "t")


def test_backtick_table(engine):
    assert engine._extract_table_from_sql(
        "SELECT * FROM `db`.`t`"
    ) == ("db", "t")


def test_cte_resolves_real_table(engine):
    """CTE-алиас не должен считаться реальной таблицей."""
    sql = "WITH x AS (SELECT * FROM real_t) SELECT * FROM x"
    assert engine._extract_table_from_sql(sql) == (None, "real_t")


def test_subquery_resolves_real_table(engine):
    sql = "SELECT * FROM (SELECT * FROM real_t) sub"
    assert engine._extract_table_from_sql(sql) == (None, "real_t")


def test_invalid_sql_returns_none(engine):
    assert engine._extract_table_from_sql("NOT SQL AT ALL") is None


def test_empty_returns_none(engine):
    assert engine._extract_table_from_sql("") is None


def test_last_table_from_last_sqls_backticks(engine):
    """Обратные кавычки не должны протекать в результат (sqlglot их снимает)."""
    state = engine.sessions.get("test-user-last-sqls")
    state.last_sqls = ["SELECT * FROM `db`.`users` LIMIT 10"]
    assert engine._extract_last_table_from_history(state) == "db.users"


def test_last_table_from_history_when_no_sqls(engine):
    state = engine.sessions.get("test-user-history")
    state.last_sqls = []
    state.history = [
        {"role": "assistant", "content": "SELECT * FROM `db`.`orders`"},
    ]
    assert engine._extract_last_table_from_history(state) == "db.orders"


def test_last_table_skips_system_tables(engine):
    state = engine.sessions.get("test-user-system")
    state.last_sqls = ["SELECT * FROM information_schema.tables"]
    state.history = []
    assert engine._extract_last_table_from_history(state) is None
