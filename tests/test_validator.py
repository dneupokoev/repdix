import sys
sys.path.insert(0, ".")

from app.core.validator import validate_sql


def test_select_ok():
    ok, reason = validate_sql("SELECT 1")
    assert ok is True
    assert reason == ""


def test_with_select_ok():
    ok, _ = validate_sql("WITH x AS (SELECT 1) SELECT * FROM x")
    assert ok is True


def test_delete_rejected():
    ok, reason = validate_sql("DELETE FROM users")
    assert ok is False
    assert "only SELECT or WITH" in reason


def test_insert_rejected():
    ok, reason = validate_sql("INSERT INTO users VALUES (1)")
    assert ok is False


def test_multiple_statements_rejected():
    ok, reason = validate_sql("SELECT 1; SELECT 2")
    assert ok is False
    assert "multiple" in reason


def test_into_outfile_rejected():
    ok, reason = validate_sql("SELECT * FROM users INTO OUTFILE '/tmp/x'")
    assert ok is False


def test_for_update_rejected():
    ok, reason = validate_sql("SELECT * FROM users FOR UPDATE")
    assert ok is False


def test_load_file_rejected():
    ok, reason = validate_sql("SELECT LOAD_FILE('/etc/passwd')")
    assert ok is False


def test_sleep_rejected():
    ok, reason = validate_sql("SELECT SLEEP(10)")
    assert ok is False


def test_benchmark_rejected():
    ok, reason = validate_sql("SELECT BENCHMARK(1000000, MD5('x'))")
    assert ok is False


def test_with_delete_rejected():
    ok, reason = validate_sql("WITH x AS (SELECT 1) DELETE FROM users WHERE id IN (SELECT * FROM x)")
    assert ok is False


def test_empty_rejected():
    ok, reason = validate_sql("")
    assert ok is False


def test_delete_in_comment_is_known_false_positive():
    # ИЗВЕСТНОЕ ограничение: DELETE в комментарии вызывает ложное срабатывание.
    # Это безопасно (лишний отказ), но нежелательно. Тест фиксирует ТЕКУЩЕЕ поведение,
    # а не требование. Если валидатор научится игнорировать комментарии — тест обновить.
    ok, _ = validate_sql("SELECT 1 /* DELETE */")
    assert ok is False


def test_select_with_semicolon_ok():
    ok, _ = validate_sql("SELECT 1;")
    assert ok is True


def test_procedure_without_parens_rejected():
    ok, _ = validate_sql("SELECT * FROM users PROCEDURE ANALYSE")
    assert ok is False
