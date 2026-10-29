"""Тесты фильтрации технических таблиц/колонок по префиксам."""

import pytest

from app.core.technical_filter import (
    get_technical_prefixes,
    is_technical,
    is_technical_qualified,
)


def test_is_technical_positive():
    assert is_technical("technical_foo") is True


def test_is_technical_negative():
    assert is_technical("normal_table") is False


def test_is_technical_case_insensitive():
    assert is_technical("TECHNICAL_BAR") is True


def test_is_technical_empty():
    assert is_technical("") is False


def test_is_technical_none():
    assert is_technical(None) is False


def test_get_technical_prefixes_custom(monkeypatch):
    monkeypatch.setenv("TECHNICAL_PREFIXES", "technical_,internal_")
    assert get_technical_prefixes() == ("technical_", "internal_")


def test_get_technical_prefixes_default(monkeypatch):
    monkeypatch.delenv("TECHNICAL_PREFIXES", raising=False)
    assert get_technical_prefixes() == ("technical_",)


def test_get_technical_prefixes_blank_env(monkeypatch):
    monkeypatch.setenv("TECHNICAL_PREFIXES", "   ")
    assert get_technical_prefixes() == ("technical_",)


def test_is_technical_custom_prefix(monkeypatch):
    monkeypatch.setenv("TECHNICAL_PREFIXES", "internal_")
    assert is_technical("internal_audit") is True
    assert is_technical("technical_foo") is False


def test_is_technical_qualified_table_part():
    assert is_technical_qualified("db.technical_logs") is True


def test_is_technical_qualified_schema_part():
    assert is_technical_qualified("technical_schema.users") is True


def test_is_technical_qualified_normal():
    assert is_technical_qualified("db.users") is False


def test_is_technical_qualified_short_name():
    assert is_technical_qualified("technical_logs") is True


def test_is_technical_qualified_empty_and_none():
    assert is_technical_qualified("") is False
    assert is_technical_qualified(None) is False
