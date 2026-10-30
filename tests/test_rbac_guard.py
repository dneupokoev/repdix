# tests/test_rbac_guard.py
dv_file_version = '261030.03'
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

USERS_COLUMNS_ROLE = """
roles:
  test_role:
    tables:
      users:
        allowed_columns: [id, name]
"""

USERS_EXCLUDED_ROLE = """
roles:
  test_role:
    tables:
      users:
        allowed_columns: "*"
        excluded_columns: [secret]
"""

USERS_WHITELIST_ROLE = """
roles:
  test_role:
    tables:
      users:
        allowed_columns: [id, name]
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


# ----------------------------------------------------------------------
# Регрессия: sqlglot отдаёт Column.table как str (а не exp.Identifier),
# поэтому обращение column.table.name падало с AttributeError.
# ----------------------------------------------------------------------
def test_qualified_column_no_crash(tmp_path):
    roles_yaml = _write_roles(tmp_path, USERS_STAR_ROLE)
    guard = RBACGuard(roles_config_path=roles_yaml)

    sql = "SELECT u.id, u.name FROM users u"
    ok, result = guard.validate_sql(sql, ["test_role"], "")

    assert ok is True
    assert result == sql


def test_qualified_star_no_crash(tmp_path):
    roles_yaml = _write_roles(tmp_path, USERS_STAR_ROLE)
    guard = RBACGuard(roles_config_path=roles_yaml)

    ok, _ = guard.validate_sql("SELECT u.* FROM users u", ["test_role"], "")

    assert ok is True


def test_qualified_technical_column_denied(tmp_path):
    roles_yaml = _write_roles(tmp_path, USERS_STAR_ROLE)
    guard = RBACGuard(roles_config_path=roles_yaml)

    ok, reason = guard.validate_sql(
        "SELECT u.technical_id FROM users u", ["test_role"], ""
    )

    assert ok is False
    assert "technical" in reason.lower()


def test_qualified_star_with_technical_columns(tmp_path):
    roles_yaml = _write_roles(tmp_path, USERS_STAR_ROLE)
    guard = RBACGuard(
        roles_config_path=roles_yaml,
        technical_columns_getter=lambda table: (
            {"technical_x"} if table.split(".")[-1] == "users" else set()
        ),
    )

    ok, reason = guard.validate_sql("SELECT u.* FROM users u", ["test_role"], "")

    assert ok is False
    reason_lower = reason.lower()
    assert "select *" in reason_lower
    assert any(
        word in reason_lower
        for word in ("forbidden", "запрещён", "запрещен", "нельзя")
    )


def test_qualified_star_with_excluded_columns(tmp_path):
    """Алиас-звёздочка: правило таблицы ищется по имени таблицы, не по алиасу."""
    roles_yaml = _write_roles(tmp_path, USERS_EXCLUDED_ROLE)
    guard = RBACGuard(roles_config_path=roles_yaml)

    ok, reason = guard.validate_sql("SELECT u.* FROM users u", ["test_role"], "")

    assert ok is False
    reason_lower = reason.lower()
    assert "select *" in reason_lower
    assert any(
        word in reason_lower
        for word in ("forbidden", "запрещён", "запрещен", "нельзя")
    )


def test_qualified_column_allowed_by_rbac(tmp_path):
    roles_yaml = _write_roles(tmp_path, USERS_COLUMNS_ROLE)
    guard = RBACGuard(roles_config_path=roles_yaml)

    ok, _ = guard.validate_sql("SELECT u.id FROM users u", ["test_role"], "")

    assert ok is True


def test_qualified_column_denied_by_rbac(tmp_path):
    roles_yaml = _write_roles(tmp_path, USERS_COLUMNS_ROLE)
    guard = RBACGuard(roles_config_path=roles_yaml)

    ok, reason = guard.validate_sql(
        "SELECT u.secret FROM users u", ["test_role"], ""
    )

    assert ok is False
    assert "secret" in reason.lower()


def test_select_star_blocked_when_allowed_columns_set_but_excluded_empty(tmp_path):
    """Регрессия: явный белый список (без excluded) не должен пропускать *."""
    roles_yaml = _write_roles(tmp_path, USERS_WHITELIST_ROLE)
    guard = RBACGuard(roles_config_path=roles_yaml)

    ok, reason = guard.validate_sql("SELECT * FROM users", ["test_role"], "")

    assert ok is False
    reason_lower = reason.lower()
    assert "select *" in reason_lower
    assert any(
        word in reason_lower
        for word in ("forbidden", "запрещён", "запрещен", "нельзя")
    )


def test_qualified_select_star_blocked_when_allowed_columns_set(tmp_path):
    """Алиас-звёздочка (u.*) тоже должна блокироваться при белом списке."""
    roles_yaml = _write_roles(tmp_path, USERS_WHITELIST_ROLE)
    guard = RBACGuard(roles_config_path=roles_yaml)

    ok, reason = guard.validate_sql(
        "SELECT u.* FROM users u", ["test_role"], ""
    )

    assert ok is False
    reason_lower = reason.lower()
    assert "select *" in reason_lower
    assert any(
        word in reason_lower
        for word in ("forbidden", "запрещён", "запрещен", "нельзя")
    )


# ----------------------------------------------------------------------
# Защита от prompt injection, приводящего к опасному SQL:
#   - системные таблицы запрещены даже для wildcard-правила ("*");
#   - парсинг fail-closed: битый SQL отклоняется.
# ----------------------------------------------------------------------
def test_p2sql_blocks_union_to_system_tables(tmp_path):
    """Любая роль (даже admin) не должна пробить information_schema."""
    roles_yaml = _write_roles(tmp_path, ADMIN_STAR_ROLE)
    guard = RBACGuard(roles_config_path=roles_yaml)

    ok, reason = guard.validate_sql(
        "SELECT * FROM information_schema.tables", ["admin"], ""
    )

    assert ok is False
    assert "системн" in reason.lower()


def test_p2sql_blocks_system_table_in_subquery(tmp_path):
    """Системная таблица в подзапросе/UNION тоже должна блокироваться."""
    roles_yaml = _write_roles(tmp_path, ADMIN_STAR_ROLE)
    guard = RBACGuard(roles_config_path=roles_yaml)

    sql = (
        "SELECT u.id FROM users u WHERE u.id IN "
        "(SELECT table_name FROM information_schema.tables)"
    )
    ok, reason = guard.validate_sql(sql, ["admin"], "")

    assert ok is False
    assert "системн" in reason.lower()


def test_p2sql_fail_closed_on_parse_error(tmp_path):
    """Битый SQL не парсится — запрос отклоняется (fail-closed)."""
    roles_yaml = _write_roles(tmp_path, ADMIN_STAR_ROLE)
    guard = RBACGuard(roles_config_path=roles_yaml)

    ok, reason = guard.validate_sql("SELECT (((", ["admin"], "")

    assert ok is False