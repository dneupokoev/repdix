"""Тесты для app.core.user_manager (сохранение users.yaml)."""

import os

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
