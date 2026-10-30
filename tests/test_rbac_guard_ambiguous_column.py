dv_file_version = '261030.04'
"""Регрессия: неквалифицированная колонка при нескольких таблицах запроса
должна быть разрешена ВО ВСЕХ таблицах (было any, стало all).
"""

from app.core.rbac_guard import RBACGuard


TWO_TABLES_ROLE = """
roles:
  test_role:
    tables:
      t1:
        allowed_columns: [secret_col, id]
      t2:
        allowed_columns: [public_col, id]
"""


def _write_roles(tmp_path, content: str) -> str:
    path = tmp_path / "roles.yaml"
    path.write_text(content, encoding="utf-8")
    return str(path)


def _make_guard(tmp_path) -> RBACGuard:
    return RBACGuard(roles_config_path=_write_roles(tmp_path, TWO_TABLES_ROLE))


def test_unqualified_column_denied_when_not_allowed_in_all(tmp_path):
    guard = _make_guard(tmp_path)

    sql = "SELECT secret_col FROM t1 JOIN t2 ON t1.id = t2.id"
    ok, reason = guard.validate_sql(sql, ["test_role"], "")

    assert ok is False
    assert "secret_col" in reason.lower()


def test_qualified_column_allowed_per_table(tmp_path):
    guard = _make_guard(tmp_path)

    sql = "SELECT t1.secret_col FROM t1 JOIN t2 ON t1.id = t2.id"
    ok, result = guard.validate_sql(sql, ["test_role"], "")

    assert ok is True
    assert result == sql


def test_unqualified_column_allowed_single_table(tmp_path):
    guard = _make_guard(tmp_path)

    sql = "SELECT secret_col FROM t1"
    ok, result = guard.validate_sql(sql, ["test_role"], "")

    assert ok is True
    assert result == sql


def test_unqualified_column_allowed_in_all_tables(tmp_path):
    guard = _make_guard(tmp_path)

    sql = "SELECT id FROM t1 JOIN t2 ON t1.id = t2.id"
    ok, result = guard.validate_sql(sql, ["test_role"], "")

    assert ok is True
    assert result == sql
