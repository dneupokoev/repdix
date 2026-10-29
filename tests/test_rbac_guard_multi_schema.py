"""Тесты multi-schema методов RBACGuard (get_allowed_tables/get_allowed_schemas)."""

from app.core.rbac_guard import RBACGuard

ROLES_YAML = """
roles:
  admin:
    tables:
      "*":
        allowed_columns: "*"
  reader_a:
    tables:
      db_a.users:
        allowed_columns: "*"
  reader_short:
    tables:
      users:
        allowed_columns: "*"
"""


def make_guard(tmp_path) -> RBACGuard:
    """Создаёт RBACGuard с временным roles.yaml."""
    path = tmp_path / "roles.yaml"
    path.write_text(ROLES_YAML, encoding="utf-8")
    return RBACGuard(roles_config_path=str(path))


# ---------------------------------------------------------------------------
# get_allowed_tables
# ---------------------------------------------------------------------------
def test_get_allowed_tables_wildcard(tmp_path):
    guard = make_guard(tmp_path)
    assert guard.get_allowed_tables(["admin"], ["db_a", "db_b"]) == {"*"}


def test_get_allowed_tables_explicit(tmp_path):
    guard = make_guard(tmp_path)
    assert guard.get_allowed_tables(["reader_a"], ["db_a", "db_b"]) == {
        "db_a.users"
    }


def test_get_allowed_tables_short(tmp_path):
    guard = make_guard(tmp_path)
    assert guard.get_allowed_tables(
        ["reader_short"], ["db_a", "db_b"], "db_a",
    ) == {"db_a.users"}


# ---------------------------------------------------------------------------
# get_allowed_schemas
# ---------------------------------------------------------------------------
def test_get_allowed_schemas_wildcard(tmp_path):
    guard = make_guard(tmp_path)
    assert guard.get_allowed_schemas(["admin"], ["db_a", "db_b"]) == {
        "db_a", "db_b",
    }


def test_get_allowed_schemas_explicit(tmp_path):
    guard = make_guard(tmp_path)
    assert guard.get_allowed_schemas(["reader_a"], ["db_a", "db_b"]) == {"db_a"}


def test_get_allowed_schemas_short(tmp_path):
    guard = make_guard(tmp_path)
    assert guard.get_allowed_schemas(
        ["reader_short"], ["db_a", "db_b"], "db_a",
    ) == {"db_a"}
