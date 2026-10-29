"""Тесты RBAC-гарда (app/core/rbac_guard.py).

Проверяем, что технические таблицы/колонки блокируются ДО применения
RBAC-правил (включая wildcard "*"), и что SELECT * по таблице со скрытыми
техническими колонками запрещён (через technical_columns_getter).
"""

from app.core.rbac_guard import RBACGuard


ALL_STAR_ROLE = """
roles:
  test_role:
    tables:
      "*":
        allowed_columns: "*"
"""

USERS_STAR_ROLE = """
roles:
  test_role:
    tables:
      users:
        allowed_columns: "*"
"""

ADMIN_STAR_ROLE = """
roles:
  admin:
    tables:
      "*":
        allowed_columns: "*"
"""


def _write_roles(tmp_path, content: str) -> str:
    """Пишет временный roles.yaml и возвращает путь к нему."""
    path = tmp_path / "roles.yaml"
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_technical_table_denied(tmp_path):
    roles_yaml = _write_roles(tmp_path, ALL_STAR_ROLE)
    guard = RBACGuard(roles_config_path=roles_yaml)

    ok, reason = guard.validate_sql(
        "SELECT * FROM technical_logs", ["test_role"], ""
    )

    assert ok is False
    assert "technical" in reason.lower()


def test_technical_column_denied(tmp_path):
    roles_yaml = _write_roles(tmp_path, ALL_STAR_ROLE)
    guard = RBACGuard(roles_config_path=roles_yaml)

    ok, reason = guard.validate_sql(
        "SELECT technical_id FROM users", ["test_role"], ""
    )

    assert ok is False
    assert "technical" in reason.lower()


def test_technical_qualified_table_denied(tmp_path):
    roles_yaml = _write_roles(tmp_path, ALL_STAR_ROLE)
    guard = RBACGuard(roles_config_path=roles_yaml)

    ok, reason = guard.validate_sql(
        "SELECT * FROM db.technical_logs", ["test_role"], ""
    )

    assert ok is False
    assert "technical" in reason.lower()


def test_allowed_query(tmp_path):
    roles_yaml = _write_roles(tmp_path, USERS_STAR_ROLE)
    guard = RBACGuard(roles_config_path=roles_yaml)

    sql = "SELECT id, name FROM users"
    ok, returned_sql = guard.validate_sql(sql, ["test_role"], "")

    assert ok is True
    # Контракт validate_sql: при успехе вторым элементом возвращается сам SQL.
    assert returned_sql == sql


def test_admin_star_rule_blocked_for_technical(tmp_path):
    roles_yaml = _write_roles(tmp_path, ADMIN_STAR_ROLE)
    guard = RBACGuard(roles_config_path=roles_yaml)

    ok, reason = guard.validate_sql(
        "SELECT * FROM technical_logs", ["admin"], ""
    )

    assert ok is False
    assert "technical" in reason.lower()


def test_select_star_with_technical_columns(tmp_path):
    roles_yaml = _write_roles(tmp_path, USERS_STAR_ROLE)
    guard = RBACGuard(
        roles_config_path=roles_yaml,
        technical_columns_getter=lambda table: (
            {"technical_x"} if table == "users" else set()
        ),
    )

    ok, reason = guard.validate_sql(
        "SELECT * FROM users", ["test_role"], ""
    )

    assert ok is False
    assert "select * forbidden" in reason.lower()
