"""Тесты порядка инициализации логирования в app/main.py.

Регрессия: _setup_logging() вызывался ДО load_dotenv(), поэтому
LOG_MAX_BYTES / SECURITY_LOG_* из .env не применялись.
"""

from __future__ import annotations

dv_file_version = '261030.01'

import asyncio

import pytest

import app.main as app_main_module


def test_log_max_bytes_from_dotenv_applied(monkeypatch) -> None:
    """load_dotenv вызывается ДО _setup_logging."""
    calls: list[str] = []

    monkeypatch.setenv("LOG_MAX_BYTES", "52428800")
    monkeypatch.setattr(
        "app.main.load_dotenv",
        lambda *args, **kwargs: calls.append("load_dotenv"),
    )
    monkeypatch.setattr(
        "app.main._setup_logging",
        lambda: calls.append("_setup_logging"),
    )

    class _StopInit(Exception):
        pass

    async def _stop_init():
        raise _StopInit()

    monkeypatch.setattr("app.main._init_services", _stop_init)

    with pytest.raises(SystemExit):
        asyncio.run(app_main_module.main())

    assert calls.index("load_dotenv") < calls.index("_setup_logging")
    assert app_main_module._env_int(
        "LOG_MAX_BYTES", 100 * 1024 * 1024,
    ) == 52428800
