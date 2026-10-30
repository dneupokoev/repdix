"""Тесты кэша text_columns в RepdixEngine._format_sql_result."""

import asyncio

from app.core.engine import RepdixEngine


def run(coro):
    return asyncio.run(coro)


class _FakeDBManager:
    """Минимальная заглушка DBManager со счётчиком вызовов get_table_schema."""

    def __init__(self, schema_info: list[dict]) -> None:
        self.schema_info = schema_info
        self.calls: list[tuple[str, str, str | None]] = []

    async def get_table_schema(self, db_name, table_name, schema=None):
        self.calls.append((db_name, table_name, schema))
        return self.schema_info


_ROWS = [{"a": "x"}]


def test_format_sql_result_caches_text_columns_for_same_table():
    db = _FakeDBManager([{"column_name": "a", "data_type": "text"}])
    engine = RepdixEngine(db_manager=db)

    run(engine._format_sql_result(_ROWS, db_name="db", schema="s", table_name="t"))
    run(engine._format_sql_result(_ROWS, db_name="db", schema="s", table_name="t"))

    assert len(db.calls) == 1
    assert db.calls[0] == ("db", "t", "s")


def test_format_sql_result_caches_text_columns_per_table():
    db = _FakeDBManager([{"column_name": "a", "data_type": "text"}])
    engine = RepdixEngine(db_manager=db)

    run(engine._format_sql_result(_ROWS, db_name="db", schema="s", table_name="t1"))
    run(engine._format_sql_result(_ROWS, db_name="db", schema="s", table_name="t2"))

    assert len(db.calls) == 2
    assert {c[1] for c in db.calls} == {"t1", "t2"}
