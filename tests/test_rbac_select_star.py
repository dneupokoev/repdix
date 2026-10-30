"""Тесты авторазвёртывания SELECT * (RBAC-гард + engine)."""

import asyncio
import time

import pytest

from app.core.engine import RepdixEngine
from app.core.rbac_guard import RBACGuard, SELECT_STAR_ERROR_MARKER


RULES = {
    "analyst": {
        "tables": {
            "db.t_orders": {"allowed_columns": ["id", "amount"]},
            "db.t_secret": {
                "allowed_columns": "*",
                "excluded_columns": ["token", "secret_key"],
            },
        }
    }
}


@pytest.fixture()
def guard(tmp_path):
    """Гард с правилами, «вмороженными» в память (без hot-reload)."""
    roles_file = tmp_path / "roles.yaml"
    roles_file.write_text("roles: {}\n", encoding="utf-8")
    g = RBACGuard(roles_config_path=str(roles_file))
    g._config = RULES
    # _mtime в будущем -> _load_config() не перечитает файл и не затрёт правила.
    g._mtime = time.time() + 10 ** 6
    return g


# ----------------------------------------------------------------------
# validate_sql: сообщения об отказе по SELECT *
# ----------------------------------------------------------------------
def test_select_star_denied_lists_allowed_columns(guard):
    ok, msg = guard.validate_sql("SELECT * FROM db.t_orders", ["analyst"], "db")

    assert ok is False
    assert SELECT_STAR_ERROR_MARKER in msg
    assert "db.t_orders" in msg
    assert "amount" in msg and "id" in msg


def test_select_star_denied_for_excluded_columns(guard):
    ok, msg = guard.validate_sql("SELECT * FROM db.t_secret", ["analyst"], "db")

    assert ok is False
    assert SELECT_STAR_ERROR_MARKER in msg
    assert "скрытые поля" in msg
    assert "secret_key" in msg and "token" in msg


def test_select_star_denial_marker_helper(guard):
    _, msg = guard.validate_sql("SELECT * FROM db.t_orders", ["analyst"], "db")
    assert guard.is_select_star_denial(msg) is True
    assert guard.is_select_star_denial("Доступ к таблице 'x' запрещен.") is False


def test_explicit_columns_still_allowed(guard):
    ok, msg = guard.validate_sql(
        "SELECT id, amount FROM db.t_orders", ["analyst"], "db",
    )
    assert ok is True, msg


# ----------------------------------------------------------------------
# expand_select_star
# ----------------------------------------------------------------------
def test_expand_select_star_uses_allowed_columns(guard):
    sql = "SELECT * FROM db.t_orders"
    expanded = guard.expand_select_star(sql, ["analyst"], "db")

    assert "*" not in expanded
    assert "amount" in expanded and "id" in expanded

    ok, msg = guard.validate_sql(expanded, ["analyst"], "db")
    assert ok is True, msg


def test_expand_select_star_excluded_uses_getter(guard):
    all_columns = ["id", "token", "created_at", "secret_key"]
    sql = "SELECT * FROM db.t_secret"
    expanded = guard.expand_select_star(
        sql, ["analyst"], "db",
        all_columns_getter=lambda name: all_columns,
    )

    assert "token" not in expanded
    assert "secret_key" not in expanded
    assert "created_at" in expanded

    ok, msg = guard.validate_sql(expanded, ["analyst"], "db")
    assert ok is True, msg


def test_expand_select_star_without_getter_returns_original(guard):
    sql = "SELECT * FROM db.t_secret"
    assert guard.expand_select_star(sql, ["analyst"], "db") == sql


def test_expand_select_star_keeps_aggregates(guard):
    sql = "SELECT COUNT(*) AS c FROM db.t_orders"
    assert guard.expand_select_star(sql, ["analyst"], "db") == sql


def test_expand_select_star_no_rules_returns_original(guard):
    sql = "SELECT * FROM db.t_orders"
    assert guard.expand_select_star(sql, ["unknown_role"], "db") == sql


def test_extract_tables(guard):
    tables = guard.extract_tables(
        "SELECT * FROM db.t_orders o JOIN db.t_secret s ON o.id = s.id",
        "db",
    )
    assert "db.t_orders" in tables
    assert "db.t_secret" in tables


# ----------------------------------------------------------------------
# engine: авторазвёртывание перед execute_query
# ----------------------------------------------------------------------
class _FakeDB:
    """Минимальная заглушка DBManager."""

    def __init__(self, columns: dict[str, list[str]]) -> None:
        self._columns = columns

    async def get_table_schema(self, db, table, schema=None):
        return [
            {"column_name": c, "data_type": "int", "column_comment": ""}
            for c in self._columns.get(table, [])
        ]


class _FakeState:
    def __init__(self, current_db: str | None = "db") -> None:
        self.user_id = "test_user"
        self.current_db = current_db


def _make_engine(columns: dict[str, list[str]]) -> RepdixEngine:
    engine = RepdixEngine(db_manager=_FakeDB(columns), default_db="db")
    engine.rbac._config = RULES
    engine.rbac._mtime = time.time() + 10 ** 6
    return engine


def test_engine_expands_select_star_before_execution():
    engine = _make_engine({"t_orders": ["id", "amount"]})
    state = _FakeState()
    sql = "SELECT * FROM db.t_orders"

    ok, msg = engine.rbac.validate_sql(sql, ["analyst"], "db")
    assert ok is False
    assert SELECT_STAR_ERROR_MARKER in msg

    expanded = asyncio.run(
        engine._try_expand_select_star(
            sql, state, ["analyst"], "db", source="manual",
        )
    )

    assert expanded is not None
    assert "*" not in expanded
    ok, msg = engine.rbac.validate_sql(expanded, ["analyst"], "db")
    assert ok is True, msg


def test_engine_expansion_returns_none_when_columns_unknown():
    engine = _make_engine({})
    state = _FakeState()
    sql = "SELECT * FROM db.t_secret"

    expanded = asyncio.run(
        engine._try_expand_select_star(
            sql, state, ["analyst"], "db", source="llm",
        )
    )
    assert expanded is None
