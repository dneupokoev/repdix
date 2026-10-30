"""Регрессия: COUNT(*) не должен блокироваться правилом "SELECT *".

COUNT(*) — агрегат, полей не возвращает, поэтому запрет SELECT *
(из-за excluded_columns / белого списка allowed_columns) к нему неприменим.
При этом COUNT(<скрытое поле>) по-прежнему запрещён.
"""

import pytest

from app.core.rbac_guard import RBACGuard


ROLES_YAML = """
roles:
  tester:
    tables:
      t:
        allowed_columns: "*"
        excluded_columns: [secret]
"""


@pytest.fixture
def roles_yaml(tmp_path):
    path = tmp_path / "roles.yaml"
    path.write_text(ROLES_YAML, encoding="utf-8")
    return path


@pytest.fixture
def guard(roles_yaml):
    return RBACGuard(roles_config_path=str(roles_yaml))


def test_count_star_allowed_with_excluded_columns(guard):
    sql = "SELECT COUNT(*) FROM t"
    ok, result = guard.validate_sql(sql, ["tester"], "")

    assert ok is True
    assert result == sql


def test_select_star_denied_with_excluded_columns(guard):
    ok, reason = guard.validate_sql("SELECT * FROM t", ["tester"], "")

    assert ok is False
    assert "нельзя SELECT *" in reason


def test_count_excluded_column_denied(guard):
    ok, reason = guard.validate_sql("SELECT COUNT(secret) FROM t", ["tester"], "")

    assert ok is False
    assert "Доступ к полю 'secret'" in reason


def test_count_allowed_column_allowed(guard):
    sql = "SELECT COUNT(id) FROM t"
    ok, result = guard.validate_sql(sql, ["tester"], "")

    assert ok is True
    assert result == sql
