"""Тесты чтения AGENT_MAX_STEPS в RepdixEngine.__init__.

Проверяем: некорректное значение переменной окружения не должно ронять
инициализацию движка, а лимит шагов агента должен быть >= 1.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from app.core.engine import RepdixEngine


def _make_engine(monkeypatch: pytest.MonkeyPatch,
                 value: str | None) -> RepdixEngine:
    """Создаёт движок с моками SessionStore/RBACGuard.

    Моки нужны, чтобы конструктор не зависел от config/*.yaml.
    """
    if value is None:
        monkeypatch.delenv("AGENT_MAX_STEPS", raising=False)
    else:
        monkeypatch.setenv("AGENT_MAX_STEPS", value)
    with patch("app.core.engine.SessionStore"):
        with patch("app.core.engine.RBACGuard"):
            return RepdixEngine()


def test_agent_max_steps_default_is_ten(monkeypatch: pytest.MonkeyPatch) -> None:
    """Переменная не задана — fallback 10."""
    engine = _make_engine(monkeypatch, None)
    assert engine.agent_max_steps == 10


def test_agent_max_steps_zero_becomes_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AGENT_MAX_STEPS="0" — минимум 1 (иначе цикл агента не выполнится)."""
    engine = _make_engine(monkeypatch, "0")
    assert engine.agent_max_steps == 1


def test_agent_max_steps_negative_becomes_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отрицательное значение тоже упирается в минимум 1."""
    engine = _make_engine(monkeypatch, "-5")
    assert engine.agent_max_steps == 1


def test_agent_max_steps_non_numeric_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AGENT_MAX_STEPS="abc" — fallback 10 и предупреждение в лог."""
    with caplog.at_level(logging.WARNING, logger="app.core.engine"):
        engine = _make_engine(monkeypatch, "abc")

    assert engine.agent_max_steps == 10
    assert "AGENT_MAX_STEPS" in caplog.text


def test_agent_max_steps_custom_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """AGENT_MAX_STEPS="7" — ровно 7 шагов."""
    engine = _make_engine(monkeypatch, "7")
    assert engine.agent_max_steps == 7
