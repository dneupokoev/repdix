dv_file_version = '261030.01'
"""Тесты фоновой очистки просроченных сессий (SessionStore)."""

import asyncio

from app.core.session import SessionStore


def test_cleanup_task_removes_expired() -> None:
    """Фоновая очистка удаляет сессию после истечения TTL."""
    async def _run() -> None:
        # ttl_sec в конструкторе ограничен снизу 60 сек — для теста
        # подменяем _ttl_sec напрямую.
        store = SessionStore(ttl_sec=60)
        store._ttl_sec = 0.1
        store._cleanup_interval_sec = 0.05

        store.get("u1")
        assert store.peek("u1") is not None

        store.start_cleanup_task()
        try:
            await asyncio.sleep(0.3)
            assert store.peek("u1") is None
        finally:
            await store.stop_cleanup_task()

    asyncio.run(_run())


def test_start_cleanup_task_is_idempotent() -> None:
    """Повторный start_cleanup_task не создаёт второй таск."""
    async def _run() -> None:
        store = SessionStore()
        store._cleanup_interval_sec = 0.05
        store.start_cleanup_task()
        task = store._cleanup_task
        try:
            store.start_cleanup_task()
            assert store._cleanup_task is task
        finally:
            await store.stop_cleanup_task()

    asyncio.run(_run())


def test_stop_cleanup_task_cancels_task() -> None:
    """stop_cleanup_task отменяет таск и обнуляет ссылку."""
    async def _run() -> None:
        store = SessionStore()
        store._cleanup_interval_sec = 0.05
        store.start_cleanup_task()
        assert store._cleanup_task is not None

        await store.stop_cleanup_task()
        assert store._cleanup_task is None

    asyncio.run(_run())


def test_stop_cleanup_task_idempotent() -> None:
    """Повторный stop_cleanup_task не падает и является no-op."""
    async def _run() -> None:
        store = SessionStore()

        # stop до start — no-op
        await store.stop_cleanup_task()
        assert store._cleanup_task is None

        store._cleanup_interval_sec = 0.05
        store.start_cleanup_task()
        assert store._cleanup_task is not None

        await store.stop_cleanup_task()
        assert store._cleanup_task is None

        # повторный stop — no-op, не падает
        await store.stop_cleanup_task()
        assert store._cleanup_task is None

    asyncio.run(_run())


def test_cleanup_keeps_sessions_with_active_task() -> None:
    """Сессии с активной задачей не удаляются, даже если TTL истёк."""
    async def _run() -> None:
        store = SessionStore(ttl_sec=60)
        store._ttl_sec = 0.1
        store._cleanup_interval_sec = 0.05

        state = store.get("busy")
        background = asyncio.create_task(asyncio.sleep(10))
        state.current_task = background

        store.start_cleanup_task()
        try:
            await asyncio.sleep(0.3)
            assert store.peek("busy") is state
        finally:
            await store.stop_cleanup_task()
            background.cancel()
            try:
                await background
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())
