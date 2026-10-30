"""Тесты проверки сложности SQL (app/core/sql_complexity.check_complexity).

Запросы формально безопасны (проходят validate_sql/RBAC), но способны
«повесить» БД или взорвать контекст. Такие запросы должны отклоняться
SQLTooComplex ДО выполнения; неразбираемый SQL — тоже (fail-closed).
"""

from __future__ import annotations

dv_file_version = '261030.01'

import pytest

from app.core.sql_complexity import SQLTooComplex, check_complexity


# 6 JOIN при лимите 5 (все с ON — отказ именно по количеству).
_SIX_JOINS_SQL = """
SELECT t1.id
FROM main.t1 AS t1
JOIN main.t2 AS t2 ON t1.id = t2.t1_id
JOIN main.t3 AS t3 ON t1.id = t3.t1_id
JOIN main.t4 AS t4 ON t1.id = t4.t1_id
JOIN main.t5 AS t5 ON t1.id = t5.t1_id
JOIN main.t6 AS t6 ON t1.id = t6.t1_id
JOIN main.t7 AS t7 ON t1.id = t7.t1_id
"""

# JOIN без ON/USING — декартово произведение.
_CARTESIAN_SQL = """
SELECT t1.id
FROM main.t1 AS t1
JOIN main.t2 AS t2
"""

# Глубина вложенности подзапросов = 4.
_DEPTH4_SQL = """
SELECT *
FROM (
    SELECT *
    FROM (
        SELECT *
        FROM (
            SELECT *
            FROM (
                SELECT 1 AS x
            ) AS level3
        ) AS level2
    ) AS level1
) AS level0
"""

# 4 UNION при лимите 3.
_FOUR_UNIONS_SQL = """
SELECT 1 AS x
UNION SELECT 2
UNION SELECT 3
UNION SELECT 4
UNION SELECT 5
"""

# Нормальный аналитический запрос: 3 JOIN с ON, глубина 2, 1 UNION.
_OK_ANALYTIC_SQL = """
SELECT a.id, b.name
FROM main.orders AS a
JOIN main.customers AS b ON a.customer_id = b.id
JOIN main.regions AS r ON b.region_id = r.id
WHERE a.id IN (
    SELECT d.order_id
    FROM (
        SELECT order_id
        FROM main.order_items
    ) AS d
)
UNION
SELECT x.id, y.name
FROM main.archived_orders AS x
JOIN main.customers AS y ON x.customer_id = y.id
"""


def test_too_many_joins_rejected() -> None:
    """6 JOIN при лимите 5 — отказ."""
    with pytest.raises(SQLTooComplex) as exc_info:
        check_complexity(_SIX_JOINS_SQL, dialect="mysql", max_joins=5)

    assert "JOIN" in exc_info.value.reason


def test_join_without_condition_rejected() -> None:
    """JOIN без ON/USING — декартово произведение, отказ."""
    with pytest.raises(SQLTooComplex) as exc_info:
        check_complexity(_CARTESIAN_SQL, dialect="mysql")

    assert "декартов" in exc_info.value.reason.lower()


def test_too_deep_subqueries_rejected() -> None:
    """Глубина вложенности 4 при лимите 3 — отказ."""
    with pytest.raises(SQLTooComplex) as exc_info:
        check_complexity(_DEPTH4_SQL, dialect="mysql", max_depth=3)

    assert "вложенность" in exc_info.value.reason.lower()


def test_too_many_unions_rejected() -> None:
    """4 UNION при лимите 3 — отказ."""
    with pytest.raises(SQLTooComplex) as exc_info:
        check_complexity(_FOUR_UNIONS_SQL, dialect="mysql", max_unions=3)

    assert "UNION" in exc_info.value.reason


def test_normal_analytic_query_passes() -> None:
    """2 JOIN с ON + подзапрос глубины 2 + 1 UNION — в пределах лимитов."""
    check_complexity(
        _OK_ANALYTIC_SQL,
        dialect="mysql",
        max_joins=5,
        max_depth=3,
        max_unions=3,
    )


def test_broken_sql_rejected_fail_closed() -> None:
    """Неразбираемый SQL отклоняется (fail-closed), а не уходит в БД."""
    with pytest.raises(SQLTooComplex) as exc_info:
        check_complexity("SELECT * FROM (", dialect="mysql")

    assert "разобрать" in exc_info.value.reason.lower()


def test_recursion_error_becomes_sql_too_complex(monkeypatch) -> None:
    """RecursionError в _max_subquery_depth → SQLTooComplex, не падение."""
    def _boom(node, depth=0):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(
        "app.core.sql_complexity._max_subquery_depth", _boom,
    )

    with pytest.raises(SQLTooComplex) as exc_info:
        check_complexity("SELECT 1", dialect="mysql")

    assert "вложенность подзапросов" in exc_info.value.reason.lower()
