"""Тесты LLMClient: кэширование клиентов, hot-reload и закрытие."""

import asyncio

import pytest
import yaml

from app.core import llm_client as llm_client_module
from app.core.llm_client import LLMClient


# ----------------------------------------------------------------------
# Вспомогательные функции
# ----------------------------------------------------------------------
def _write_models_config(path, m1_base_url: str = "http://localhost:1234/v1") -> None:
    """Записывает тестовый config/models.yaml с моделями m1 и m2."""
    data = {
        "models": {
            "m1": {
                "base_url": m1_base_url,
                "api_key": "test",
                "model_name": "m1",
                "supports_tools": True,
                "context_length": 8192,
            },
            "m2": {
                "base_url": "http://localhost:1234/v1",
                "api_key": "test",
                "model_name": "m2",
                "supports_tools": True,
                "context_length": 8192,
            },
        }
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


class FakeAsyncOpenAI:
    """Фейковый AsyncOpenAI: не делает HTTP-запросов, считает close()."""

    instances: list["FakeAsyncOpenAI"] = []

    def __init__(self, base_url=None, api_key=None):
        self.base_url = base_url
        self.api_key = api_key
        self.close_calls = 0
        type(self).instances.append(self)

    async def close(self) -> None:
        self.close_calls += 1


def _install_fake_async_openai(monkeypatch) -> type[FakeAsyncOpenAI]:
    """Подменяет AsyncOpenAI в модуле llm_client на фейк."""
    FakeAsyncOpenAI.instances = []
    monkeypatch.setattr(llm_client_module, "AsyncOpenAI", FakeAsyncOpenAI)
    return FakeAsyncOpenAI


# ----------------------------------------------------------------------
# Фикстуры
# ----------------------------------------------------------------------
@pytest.fixture()
def models_yaml(tmp_path):
    """Создаёт временный models.yaml и возвращает путь к нему."""
    path = tmp_path / "models.yaml"
    _write_models_config(path)
    return path


# ----------------------------------------------------------------------
# Тесты
# ----------------------------------------------------------------------
def test_get_client_creates_and_caches(models_yaml, monkeypatch):
    """Повторный get_client("m1") возвращает тот же объект."""
    fake_cls = _install_fake_async_openai(monkeypatch)

    llm = LLMClient(config_path=str(models_yaml))

    client1, _, _ = llm.get_client("m1")
    client2, _, _ = llm.get_client("m1")

    assert client1 is client2
    assert id(client1) == id(client2)
    assert len(fake_cls.instances) == 1

    asyncio.run(llm.close())


def test_get_client_reloads_on_config_change(models_yaml, monkeypatch):
    """После изменения конфига создаётся новый клиент, старый закрывается."""
    _install_fake_async_openai(monkeypatch)

    async def scenario():
        llm = LLMClient(config_path=str(models_yaml))
        old, _, _ = llm.get_client("m1")

        _write_models_config(models_yaml, m1_base_url="http://localhost:5678/v1")
        llm.load_config(force=True)

        new, _, _ = llm.get_client("m1")
        # Даём возможность выполниться задаче закрытия старого клиента.
        await asyncio.sleep(0)
        return llm, old, new

    llm, old, new = asyncio.run(scenario())

    try:
        assert new is not old
        assert old.close_calls == 1
    finally:
        asyncio.run(llm.close())


def test_orphaned_clients_on_no_loop(models_yaml, monkeypatch):
    """Без running loop старый клиент закрывается при llm.close()."""
    _install_fake_async_openai(monkeypatch)

    # Синхронный контекст: running loop отсутствует.
    llm = LLMClient(config_path=str(models_yaml))
    old, _, _ = llm.get_client("m1")

    _write_models_config(models_yaml, m1_base_url="http://localhost:5678/v1")
    llm.load_config(force=True)

    new, _, _ = llm.get_client("m1")

    assert new is not old
    # Пока llm.close() не вызван, orphaned-клиент не закрыт.
    assert old.close_calls == 0

    asyncio.run(llm.close())

    assert old.close_calls == 1
    assert new.close_calls == 1


def test_close_closes_all_clients(models_yaml, monkeypatch):
    """close() закрывает все созданные клиенты."""
    _install_fake_async_openai(monkeypatch)

    llm = LLMClient(config_path=str(models_yaml))

    client1, _, _ = llm.get_client("m1")
    client2, _, _ = llm.get_client("m2")

    assert client1 is not client2

    asyncio.run(llm.close())

    assert client1.close_calls == 1
    assert client2.close_calls == 1
