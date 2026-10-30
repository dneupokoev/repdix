dv_file_version = '261030.02'
r"""Тесты обработки SQL-комментариев в app.core.validator.

Проверяем, что `--` (в т.ч. без пробела после, как в SQLite) и `#`
вырезаются только вне строковых литералов, а множественные запросы
по-прежнему блокируются.
"""

import pytest

from app.core.validator import _strip_comments_for_check, validate_sql


@pytest.mark.parametrize(
    "sql",
    [
        # `--` без пробела после — валидный комментарий в SQLite
        "SELECT 1 --no space",
        # Комментарий с запрещённым словом должен быть вырезан
        "SELECT 1 -- DROP TABLE x",
        # `--` внутри строкового литерала — НЕ комментарий
        "SELECT '--not a comment' AS x",
    ],
)
def test_comment_queries_allowed(sql: str) -> None:
    ok, reason = validate_sql(sql)
    assert ok, f"ожидался True для {sql!r}, получено: {reason}"


def test_multiple_statements_still_blocked() -> None:
    ok, reason = validate_sql("SELECT 1; -- DROP")
    assert not ok
    assert "multiple statements" in reason


def test_strip_comment_without_space() -> None:
    """Регрессия: `--` без пробела должен вырезаться из проверяемой строки."""
    stripped = _strip_comments_for_check("SELECT 1 --DROP TABLE x").upper()
    assert "DROP" not in stripped
    assert stripped.startswith("SELECT 1")


def test_strip_does_not_touch_string_literal() -> None:
    stripped = _strip_comments_for_check("SELECT '--DROP TABLE x' AS y")
    assert "--DROP TABLE x" in stripped
