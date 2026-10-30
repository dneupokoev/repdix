dv_file_version = '261030.01'
"""Тесты унификации размеров пула: PostgreSQL читает minsize/maxsize."""

import asyncio

import asyncpg
import yaml

from app.core.db_manager import DBManager


def run(coro):
    return asyncio.run(coro)


def _make_config(tmp_path, pg_conf: dict) -> str:
    path = tmp_path / "databases.yaml"
    path.write_text(
        yaml.safe_dump({"databases": {"pg_test": pg_conf}}, allow_unicode=True),
        encoding="utf-8",
    )
    return str(path)


def _base_pg_conf() -> dict:
    return {
        "type": "postgresql",
        "host": "localhost",
        "port": 5432,
        "user": "postgres",
        "password": "secret",
        "database": "postgres",
    }


def _patch_create_pool(monkeypatch, captured: dict) -> None:
    async def fake_create_pool(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)


def test_pg_pool_reads_minsize_maxsize(tmp_path, monkeypatch) -> None:
    """min_size/max_size в create_pool берутся из minsize/maxsize."""
    captured: dict = {}

    async def _run() -> None:
        conf = _base_pg_conf()
        conf["minsize"] = 2
        conf["maxsize"] = 7
        _patch_create_pool(monkeypatch, captured)
        manager = DBManager(config_path=_make_config(tmp_path, conf))
        await manager.connect("pg_test")

    run(_run())

    assert captured["min_size"] == 2
    assert captured["max_size"] == 7


def test_pg_pool_defaults_when_sizes_absent(tmp_path, monkeypatch) -> None:
    """Без minsize/maxsize используются дефолты 1/5."""
    captured: dict = {}

    async def _run() -> None:
        _patch_create_pool(monkeypatch, captured)
        manager = DBManager(config_path=_make_config(tmp_path, _base_pg_conf()))
        await manager.connect("pg_test")

    run(_run())

    assert captured["min_size"] == 1
    assert captured["max_size"] == 5
