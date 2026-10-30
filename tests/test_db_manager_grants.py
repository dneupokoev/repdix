"""Тесты проверки прав MySQL-пользователя (SHOW GRANTS) в DBManager.

README 4.1: readonly-режим не заменяет права БД. При старте бот пишет
WARNING в security.log, если у пользователя есть права на запись.
Проверка не должна блокировать запуск и не должна бросать исключений.
"""

from __future__ import annotations

dv_file_version = '261030.01'

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

from app.core.db_manager import DBManager


def _db_conf(**overrides) -> dict:
    """Минимальный валидный конфиг MySQL-БД для тестов."""
    conf = {
        "type": "mysql",
        "host": "localhost",
        "port": 3306,
        "user": "repdix_ro",
        "password": "secret",
        "database": "db_analysis",
        "readonly": True,
        "is_mariadb": False,
    }
    conf.update(overrides)
    return conf


class _FakeCursor:
    """Курсор aiomysql: execute/fetchall + async context manager."""

    def __init__(self, rows=None, error=None) -> None:
        self._rows = rows or []
        self._error = error

    async def execute(self, *args, **kwargs) -> None:
        if self._error is not None:
            raise self._error

    async def fetchall(self):
        return self._rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeConn:
    """Соединение aiomysql: cursor() + async context manager."""

    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeAcquire:
    """Результат pool.acquire() — async context manager."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakePool:
    """Минимальный пул: только acquire() (нужен _check_mysql_grants)."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


def _fake_pool(rows=None, error=None) -> _FakePool:
    """Собирает fake-пул с заданным ответом (или ошибкой) на SHOW GRANTS."""
    cursor = _FakeCursor(rows=rows, error=error)
    return _FakePool(_FakeConn(cursor))


def _make_manager(tmp_path: Path, db_conf: dict) -> DBManager:
    """DBManager с временным databases.yaml (одна БД main_db)."""
    config_path = tmp_path / "databases.yaml"
    config_path.write_text(
        yaml.safe_dump({"databases": {"main_db": db_conf}}, allow_unicode=True),
        encoding="utf-8",
    )
    return DBManager(config_path=str(config_path))


def _connect(tmp_path: Path, rows=None, error=None) -> DBManager:
    """Вызывает connect('main_db') с замоканным aiomysql.create_pool."""
    manager = _make_manager(tmp_path, _db_conf())
    with patch(
        "app.core.db_manager.aiomysql.create_pool",
        new=AsyncMock(return_value=_fake_pool(rows=rows, error=error)),
    ):
        asyncio.run(manager.connect("main_db"))
    return manager


def _security_warnings(caplog) -> list[str]:
    """Текст всех WARNING из логгера 'security'."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "security" and record.levelno == logging.WARNING
    ]


def _app_warnings(caplog) -> list[str]:
    """Текст всех WARNING из app-логгера db_manager."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.core.db_manager"
        and record.levelno == logging.WARNING
    ]


def test_readonly_grants_no_security_warning(tmp_path: Path, caplog) -> None:
    """SELECT/SHOW VIEW — прав на запись нет, WARNING в security нет."""
    caplog.set_level(logging.WARNING, logger="security")

    _connect(
        tmp_path,
        rows=[{
            "Grants for repdix_ro@%":
                "GRANT SELECT, SHOW VIEW ON `db_analysis`.* "
                "TO `repdix_ro`@`%`",
        }],
    )

    assert not _security_warnings(caplog)


def test_all_privileges_logs_security_warning(tmp_path: Path, caplog) -> None:
    """ALL PRIVILEGES — WARNING в security.log с 'write privileges'."""
    caplog.set_level(logging.WARNING, logger="security")

    _connect(
        tmp_path,
        rows=[{
            "Grants for root@%":
                "GRANT ALL PRIVILEGES ON *.* TO `root`@`%`",
        }],
    )

    warnings = _security_warnings(caplog)
    assert any("write privileges" in msg for msg in warnings), (
        f"ожидался WARNING про write privileges, получено: {warnings}"
    )


def test_insert_privilege_logs_security_warning(
    tmp_path: Path, caplog,
) -> None:
    """Один write-привилей (INSERT) уже даёт WARNING в security.log."""
    caplog.set_level(logging.WARNING, logger="security")

    _connect(
        tmp_path,
        rows=[{
            "Grants for repdix@%":
                "GRANT SELECT, INSERT ON `db_analysis`.* TO `repdix`@`%`",
        }],
    )

    warnings = _security_warnings(caplog)
    assert any("write privileges" in msg for msg in warnings), (
        f"ожидался WARNING про write privileges, получено: {warnings}"
    )


def test_show_grants_failure_does_not_break_connect(
    tmp_path: Path, caplog,
) -> None:
    """Ошибка SHOW GRANTS: WARNING в app-логгер, connect() не падает."""
    caplog.set_level(logging.WARNING, logger="security")
    caplog.set_level(logging.WARNING, logger="app.core.db_manager")

    manager = _connect(tmp_path, error=RuntimeError("access denied"))

    assert "main_db" in manager.pools, "connect() должен создать пул"
    assert not _security_warnings(caplog), (
        "ошибка проверки не должна попадать в security.log"
    )
    app_warnings = _app_warnings(caplog)
    assert any(
        "Не удалось проверить SHOW GRANTS" in msg for msg in app_warnings
    ), f"ожидался WARNING в app-логгере, получено: {app_warnings}"


def test_file_privilege_logs_security_warning(
    tmp_path: Path, caplog,
) -> None:
    """Админский FILE — WARNING в security.log."""
    caplog.set_level(logging.WARNING, logger="security")

    _connect(
        tmp_path,
        rows=[{
            "Grants for repdix@%":
                "GRANT SELECT, FILE ON *.* TO `repdix`@`%`",
        }],
    )

    warnings = _security_warnings(caplog)
    assert any("write privileges" in msg for msg in warnings), (
        f"ожидался WARNING про write privileges, получено: {warnings}"
    )
