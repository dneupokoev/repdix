"""Проверка is_active в engine.process_message."""

from __future__ import annotations

dv_file_version = '261030.01'

import asyncio
from unittest.mock import MagicMock

import pytest

from app.core.engine import RepdixEngine
from app.interfaces.base import ChatMessage


def run(coro):
    """Синхронный запуск корутины (как в остальных тестах проекта)."""
    return asyncio.run(coro)


def _make_engine(user_manager) -> RepdixEngine:
    return RepdixEngine(
        db_manager=None, llm_client=None, cache_manager=None,
        user_manager=user_manager,
    )


def _msg(text: str) -> ChatMessage:
    return ChatMessage(user_id="123", chat_id="123", text=text)


def test_blocked_user_gets_pending_message():
    um = MagicMock()
    um.is_admin.return_value = False
    um.is_user_active.return_value = False
    engine = _make_engine(um)
    resp = run(engine.process_message(_msg("/settings")))
    assert "заявка на рассмотрении" in resp.text.lower()


def test_admin_skips_active_check():
    um = MagicMock()
    um.is_admin.return_value = True
    um.is_user_active.return_value = False
    engine = _make_engine(um)
    resp = run(engine.process_message(_msg("/settings")))
    # Админ не должен получать "заявка на рассмотрении".
    assert "заявка на рассмотрении" not in resp.text.lower()


def test_active_user_skips_pending_message():
    um = MagicMock()
    um.is_admin.return_value = False
    um.is_user_active.return_value = True
    um.get_user_roles.return_value = ["sales_manager"]
    engine = _make_engine(um)
    resp = run(engine.process_message(_msg("/settings")))
    assert "заявка на рассмотрении" not in resp.text.lower()


def test_start_command_skips_active_check():
    um = MagicMock()
    um.is_admin.return_value = False
    um.is_user_active.return_value = False
    engine = _make_engine(um)
    # /start не должен блокироваться по is_active (нужен для регистрации).
    resp = run(engine.process_message(_msg("/start")))
    assert "заявка на рассмотрении" not in resp.text.lower()


@pytest.mark.asyncio
async def test_active_user_without_sql_executor_cannot_sql():
    um = MagicMock()
    um.is_admin.return_value = False
    um.is_user_active.return_value = True
    um.get_user_roles.return_value = ["sales_manager"]
    engine = _make_engine(um)
    resp = await engine.process_message(_msg("/sql SELECT 1"))
    assert "sql_executor" in resp.text.lower()


@pytest.mark.asyncio
async def test_active_user_with_sql_executor_passes_role_check():
    um = MagicMock()
    um.is_admin.return_value = False
    um.is_user_active.return_value = True
    um.get_user_roles.return_value = ["sales_manager", "sql_executor"]
    engine = _make_engine(um)
    resp = await engine.process_message(_msg("/sql SELECT 1"))
    # Не блокируется по is_active или sql_executor. Дальше — "выберите БД"
    # (current_db=None), либо RBAC по таблицам.
    assert "sql_executor" not in resp.text.lower()
    assert "заявка на рассмотрении" not in resp.text.lower()


@pytest.mark.asyncio
async def test_admin_can_sql_without_sql_executor():
    um = MagicMock()
    um.is_admin.return_value = True
    um.is_user_active.return_value = False
    engine = _make_engine(um)
    resp = await engine.process_message(_msg("/sql SELECT 1"))
    assert "sql_executor" not in resp.text.lower()
    assert "заявка на рассмотрении" not in resp.text.lower()
