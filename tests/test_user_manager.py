"""Тесты для app.core.user_manager (сохранение users.yaml)."""

import os

import pytest

from app.core.user_manager import UserManager


def test_save_users_yaml_calls_fsync(tmp_path, monkeypatch):
    """При сохранении users.yaml должен вызываться os.fsync."""
    calls: list[int] = []

    def fake_fsync(fd: int) -> None:
        calls.append(fd)

    monkeypatch.setattr(os, "fsync", fake_fsync)

    yaml_path = tmp_path / "users.yaml"
    manager = UserManager(str(yaml_path))

    result = manager._save_users_yaml({"1": {"id": "1"}})

    assert result is True
    assert len(calls) >= 1, "os.fsync должен вызываться при сохранении users.yaml"
    assert yaml_path.exists()


def test_save_users_yaml_fsync_oserror_does_not_fail(tmp_path, monkeypatch):
    """OSError внутри fsync не должен ломать сохранение (запись уже прошла)."""

    def failing_fsync(fd: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(os, "fsync", failing_fsync)

    yaml_path = tmp_path / "users.yaml"
    manager = UserManager(str(yaml_path))

    result = manager._save_users_yaml({"1": {"id": "1"}})

    assert result is True, "Ошибка fsync не должна приводить к неудаче сохранения"
    assert yaml_path.exists()


@pytest.mark.asyncio
async def test_reject_user_clears_roles(tmp_path):
    """reject_user очищает roles (блокировка = отзыв доступа)."""
    users_yaml = tmp_path / "users.yaml"
    users_yaml.write_text("users: []", encoding="utf-8")

    um = UserManager(users_yaml_path=str(users_yaml))
    await um.add_user("123", "user", "Иванов Иван", "IT", "тест")
    await um.approve_user("123", roles=["sales_manager", "sql_executor"])

    assert um.get_user_roles("123") == ["sales_manager", "sql_executor"]

    await um.reject_user("123")

    user = um.get_user("123")
    assert user["is_active"] is False
    assert user["roles"] == []


@pytest.mark.asyncio
async def test_toggle_user_active_clears_roles_on_block(tmp_path):
    """toggle_user_active (блокировка) очищает roles."""
    users_yaml = tmp_path / "users.yaml"
    users_yaml.write_text("users: []", encoding="utf-8")

    um = UserManager(users_yaml_path=str(users_yaml))
    await um.add_user("123", "user", "Иванов Иван", "IT", "тест")
    await um.approve_user("123", roles=["sales_manager", "sql_executor"])

    ok, new_active = await um.toggle_user_active("123")

    assert ok is True
    assert new_active is False
    user = um.get_user("123")
    assert user["roles"] == []
