dv_file_version = '261030.01'
"""Тесты: _env_int не роняет приложение на нечисловых env-значениях."""

import logging

from app.main import _env_int

ENV_NAME = "REPDIX_TEST_LOG_MAX_BYTES"


def test_invalid_value_falls_back_with_warning(monkeypatch, caplog) -> None:
    monkeypatch.setenv(ENV_NAME, "abc")
    with caplog.at_level(logging.WARNING, logger="app.main"):
        assert _env_int(ENV_NAME, 10) == 10
    messages = [r.getMessage() for r in caplog.records]
    assert any(ENV_NAME in m and "abc" in m for m in messages)


def test_zero_clamped_to_minimum(monkeypatch, caplog) -> None:
    """X="0" клампится до minimum (не до default)."""
    monkeypatch.setenv(ENV_NAME, "0")
    with caplog.at_level(logging.WARNING, logger="app.main"):
        assert _env_int(ENV_NAME, 10) == 1
        assert _env_int(ENV_NAME, 10, minimum=5) == 5
    # WARNING не пишется: "0" — валидное число, клампится молча.
    assert not caplog.records


def test_negative_clamped_to_minimum(monkeypatch, caplog) -> None:
    """Отрицательное значение клампится до minimum."""
    monkeypatch.setenv(ENV_NAME, "-7")
    with caplog.at_level(logging.WARNING, logger="app.main"):
        assert _env_int(ENV_NAME, 10) == 1
        assert _env_int(ENV_NAME, 10, minimum=3) == 3
    assert not caplog.records


def test_empty_string_uses_default(monkeypatch, caplog) -> None:
    monkeypatch.setenv(ENV_NAME, "   ")
    with caplog.at_level(logging.WARNING, logger="app.main"):
        assert _env_int(ENV_NAME, 10) == 10
    assert caplog.records == []


def test_valid_value_parsed(monkeypatch, caplog) -> None:
    monkeypatch.setenv(ENV_NAME, "42")
    with caplog.at_level(logging.WARNING, logger="app.main"):
        assert _env_int(ENV_NAME, 10) == 42
    assert caplog.records == []


def test_missing_env_uses_default(monkeypatch, caplog) -> None:
    monkeypatch.delenv(ENV_NAME, raising=False)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        assert _env_int(ENV_NAME, 10) == 10
    assert caplog.records == []
