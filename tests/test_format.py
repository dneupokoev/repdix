import sys
sys.path.insert(0, ".")

from app.core.engine import RepdixEngine


def make_engine():
    return RepdixEngine()


def test_empty_rows():
    e = make_engine()
    assert e._format_sql_result([]) == "Результат: пусто"


def test_single_row_small_cols():
    e = make_engine()
    out = e._format_sql_result([{"id": 1, "name": "x"}])
    assert "| id | name |" in out
    assert "| 1 | x |" in out
    assert not out.startswith("1. ")   # не списочный формат


def test_many_cols_uses_list():
    e = make_engine()
    row = {f"c{i}": i for i in range(10)}
    out = e._format_sql_result([row])
    assert "| c0 |" not in out   # табличного заголовка нет
    assert out.startswith("1. ")   # списочный формат
    assert "c0: 0" in out


def test_truncation_by_rows():
    e = make_engine()
    e.max_rows_in_response = 2
    rows = [{"id": i} for i in range(5)]
    out = e._format_sql_result(rows)
    assert "показаны первые 2 из 5" in out


def test_truncation_by_length():
    e = make_engine()
    e.max_rows_in_response = 1000   # чтобы сработала обрезка по длине, а не по строкам
    e.max_message_length = 200
    rows = [{"a": "y" * 40, "b": "z" * 40} for _ in range(20)]
    out = e._format_sql_result(rows)
    assert len(out) <= 200
    assert "сокращено" in out


def test_truncation_by_length_table_format():
    e = make_engine()
    e.max_rows_in_response = 1000
    e.max_message_length = 200
    rows = [{"id": i, "name": "x" * 40} for i in range(100)]
    out = e._format_sql_result(rows)
    assert len(out) <= 200
    assert "сокращено" in out
    assert "| id |" in out   # табличный формат (2 колонки < 5)
