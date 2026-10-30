"""Защита в глубину: rbac_msg не подставляется в sql, если это не SQL.

validate_sql() сейчас возвращает (True, sql) при успехе, но контракт может
измениться (например, на (True, "")). Тесты фиксируют, что engine НЕ подставит
в execute_query пустую строку или текст сообщения об ошибке.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.core import engine as engine_module
from app.core.engine import RepdixEngine
from app.interfaces.base import ChatMessage


def run(coro):
    """Синхронный запуск корутины (конвенция tests/test_db_manager_schemas.py)."""
    return asyncio.run(coro)


def _make_engine(rbac_result):
    """Минимальный RepdixEngine с замоканными DB/RBAC.

    Args:
        rbac_result: что вернёт rbac.validate_sql().
    """
    db_manager = MagicMock()
    db_manager.get_db_config.return_value = {"type": "mysql", "database": "testdb"}
    db_manager.get_db_type.return_value = "mysql"
    db_manager.get_db_names.return_value = ["testdb"]
    db_manager.get_db_display_name.side_effect = lambda n: n
    db_manager.execute_query = AsyncMock(return_value=[])
    db_manager.get_schemas = AsyncMock(return_value=["testdb"])
    db_manager.get_table_list = AsyncMock(return_value=[])
    db_manager.get_table_schema = AsyncMock(return_value=[])

    user_manager = MagicMock()
    user_manager.get_user_roles.return_value = ["analyst"]

    engine = RepdixEngine(
        db_manager=db_manager,
        llm_client=None,
        default_db="testdb",
        user_manager=user_manager,
    )
    # Instance-атрибут перекрывает метод RBACGuard — реальный конфиг не читается.
    engine.rbac.validate_sql = MagicMock(return_value=rbac_result)
    return engine, db_manager


def _message(text):
    return ChatMessage(text=text, user_id="u1", chat_id="c1")


def _silence_manual_sql(monkeypatch):
    """Не пишем в файл аудита ручных SQL во время тестов."""
    monkeypatch.setattr(engine_module, "log_manual_sql", lambda **kwargs: None)


def test_empty_rbac_msg_does_not_blank_sql(monkeypatch):
    """validate_sql -> (True, "") не должен обнулять SQL в execute_query."""
    engine, db_manager = _make_engine((True, ""))
    _silence_manual_sql(monkeypatch)

    response = run(engine.process_message(_message("/sql SELECT 1")))

    assert db_manager.execute_query.await_count == 1
    assert db_manager.execute_query.await_args.args[1] == "SELECT 1"
    assert response.text


def test_error_text_rbac_msg_is_not_used_as_sql(monkeypatch):
    """validate_sql -> (True, "<текст ошибки>") не подменяет SQL этим текстом."""
    engine, db_manager = _make_engine((True, "Доступ к таблице 'x' запрещен."))
    _silence_manual_sql(monkeypatch)

    run(engine.process_message(_message("/sql SELECT 1")))

    assert db_manager.execute_query.await_count == 1
    assert db_manager.execute_query.await_args.args[1] == "SELECT 1"


def test_whitespace_only_rbac_msg_does_not_blank_sql(monkeypatch):
    """validate_sql -> (True, "   ") не должен подменять SQL пробелами."""
    engine, db_manager = _make_engine((True, "   "))
    _silence_manual_sql(monkeypatch)

    run(engine.process_message(_message("/sql SELECT 1")))

    assert db_manager.execute_query.await_args.args[1] == "SELECT 1"


def test_normalized_sql_rbac_msg_is_still_used(monkeypatch):
    """Нормализованный SQL из validate_sql по-прежнему подставляется."""
    expanded = "SELECT `id` FROM `testdb`.`t`"
    engine, db_manager = _make_engine((True, expanded))
    _silence_manual_sql(monkeypatch)

    run(engine.process_message(_message("/sql SELECT * FROM t")))

    assert db_manager.execute_query.await_count == 1
    assert db_manager.execute_query.await_args.args[1] == expanded
