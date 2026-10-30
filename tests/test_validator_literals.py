dv_file_version = '261030.01'
r"""Тесты: DANGEROUS_PATTERNS ищутся только вне строковых литералов.

FORBIDDEN_KEYWORDS по-прежнему проверяются по всему тексту
(INSERT внутри литерала считается обфускацией и блокируется).
"""

from app.core.validator import validate_sql


def test_set_global_in_single_quoted_literal_allowed() -> None:
    ok, reason = validate_sql("SELECT 'SET GLOBAL x'")
    assert ok is True, reason
    assert reason == ""


def test_set_session_in_double_quoted_literal_allowed() -> None:
    ok, reason = validate_sql('SELECT "SET SESSION y"')
    assert ok is True, reason
    assert reason == ""


def test_sleep_in_literal_allowed() -> None:
    ok, reason = validate_sql("SELECT 'SLEEP(5)'")
    assert ok is True, reason
    assert reason == ""


def test_sleep_outside_literal_rejected() -> None:
    ok, reason = validate_sql("SELECT SLEEP(5)")
    assert ok is False
    assert reason.startswith("forbidden pattern:")


def test_insert_in_literal_still_rejected() -> None:
    # INSERT внутри литерала — оставляем блок (обфускация).
    ok, reason = validate_sql("SELECT 'INSERT INTO t VALUES (1)'")
    assert ok is False
    assert reason == "forbidden keyword: INSERT"


def test_doubled_quote_in_literal_allowed() -> None:
    ok, reason = validate_sql("SELECT 'a''b' FROM t")
    assert ok is True, reason
    assert reason == ""
