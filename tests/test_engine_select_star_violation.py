"""Тесты поведения _try_expand_select_star().

Неудача авторазвёртывания SELECT * — не нарушение безопасности:
пишем в audit.log (type=expand_select_star_failed), а не в security.log.
Отказ повторной RBAC-валидации — реальный инцидент (log_violation).
Проверяются три ветки: неудача развёртывания, отказ RBAC, успех.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.engine import RepdixEngine


def _make_engine() -> RepdixEngine:
    """Движок без БД/LLM/Redis: конструктор не требует config-файлов."""
    with patch("app.core.engine.SessionStore"):
        with patch("app.core.engine.RBACGuard"):
            return RepdixEngine()


def test_expand_failure_logs_audit_not_security(caplog) -> None:
    """db_manager=None — развернуть нельзя: audit.log, НЕ security.log."""
    engine = _make_engine()
    state = SimpleNamespace(user_id="user-1", current_db=None)

    with caplog.at_level(logging.INFO, logger="audit"):
        with patch("app.core.engine.log_violation") as log_mock:
            result = asyncio.run(
                engine._try_expand_select_star(
                    "SELECT * FROM t", state, ["user"], "public",
                    source="llm",
                )
            )

    assert result is None
    log_mock.assert_not_called()

    records = [
        json.loads(r.message) for r in caplog.records
        if r.name == "audit"
    ]
    failures = [
        r for r in records if r.get("type") == "expand_select_star_failed"
    ]
    assert len(failures) == 1
    event = failures[0]
    assert event["user_id"] == "user-1"
    assert event["source"] == "llm"
    assert event["sql"] == "SELECT * FROM t"
    assert "timestamp" in event


def test_expand_rejected_by_rbac_logs_violation() -> None:
    """Развёрнутый SQL не прошёл повторную RBAC-валидацию — violation."""
    engine = _make_engine()
    engine.rbac = MagicMock()
    engine.rbac.validate_sql.return_value = (False, "table not allowed")
    state = SimpleNamespace(user_id="user-2", current_db="main_db")
    expanded_sql = "SELECT a FROM t"

    with patch.object(
        engine, "_expand_select_star_sql",
        new=AsyncMock(return_value=expanded_sql),
    ):
        with patch("app.core.engine.log_violation") as log_mock:
            result = asyncio.run(
                engine._try_expand_select_star(
                    "SELECT * FROM t", state, ["user"], "public",
                    source="manual",
                )
            )

    assert result is None
    log_mock.assert_called_once()
    args = log_mock.call_args.args
    assert args[0] == "user-2"
    assert args[1] == expanded_sql
    assert "expand_select_star rejected" in args[2]


def test_expand_success_is_logged_separately() -> None:
    """Успешное развёртывание: log_violation НЕ вызывается, пишется audit."""
    engine = _make_engine()
    engine.rbac = MagicMock()
    expanded_sql = "SELECT a FROM t"
    engine.rbac.validate_sql.return_value = (True, expanded_sql)
    state = SimpleNamespace(user_id="user-3", current_db="main_db")

    with patch.object(
        engine, "_expand_select_star_sql",
        new=AsyncMock(return_value=expanded_sql),
    ):
        with patch.object(engine, "_log_star_expansion") as star_mock:
            with patch("app.core.engine.log_violation") as log_mock:
                result = asyncio.run(
                    engine._try_expand_select_star(
                        "SELECT * FROM t", state, ["user"], "public",
                        source="llm",
                    )
                )

    assert result == expanded_sql
    log_mock.assert_not_called()
    star_mock.assert_called_once()
