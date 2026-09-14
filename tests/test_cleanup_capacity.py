import asyncio
import threading

import pytest

from claude_native_bridge import api
from claude_native_bridge.api import Owners


async def _never():
    await asyncio.Event().wait()


class DelayedResourceEngine:
    def __init__(self):
        self.close_started = threading.Event()
        self.release = threading.Event()
        self.cleanup_confirmed = False
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self.close_started.set()
        self.release.wait(2)
        self.cleanup_confirmed = True


class ResourceFreeHungEngine:
    cleanup_resource_free = True

    def __init__(self):
        self.close_started = threading.Event()
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self.close_started.set()
        threading.Event().wait()


class FailingCloseEngine:
    cleanup_confirmed = False

    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        raise RuntimeError("synthetic close failure")


class LateProofHungEngine:
    def __init__(self):
        self.cleanup_confirmed = False
        self.close_started = threading.Event()

    def close(self):
        self.close_started.set()
        threading.Event().wait()


def test_live_resource_keeps_capacity_until_cleanup_is_confirmed(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "CLOSE_TIMEOUT_SECONDS", 0.02)

    async def run():
        delayed = DelayedResourceEngine()
        engines = iter((delayed, object()))
        owners = Owners(lambda **_: next(engines), tmp_path, limit=1)
        owner, _ = owners.admit("a", "request-a", False)
        owner.task = asyncio.create_task(_never())

        started = asyncio.get_running_loop().time()
        await owners.finish("a", owner, False)
        assert asyncio.get_running_loop().time() - started < 0.15
        assert await asyncio.to_thread(delayed.close_started.wait, 1)
        assert owner.engine is delayed
        assert owner.close_task is not None and not owner.close_task.done()
        with pytest.raises(api.HTTPException) as full:
            owners.admit("b", "request-b", False)
        assert full.value.status_code == 429

        delayed.release.set()
        await asyncio.wait_for(asyncio.shield(owner.retirement_task), 1)
        assert owner.engine is None
        admitted, _ = owners.admit("b", "request-b", False)
        assert admitted is owners.items["b"]
        assert delayed.close_calls == 1

    asyncio.run(run())


def test_slow_cleanup_does_not_consume_an_independent_spare_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "CLOSE_TIMEOUT_SECONDS", 0.02)

    async def run():
        delayed = DelayedResourceEngine()
        second = object()
        engines = iter((delayed, second))
        owners = Owners(lambda **_: next(engines), tmp_path, limit=2)
        owner, _ = owners.admit("a", "request-a", False)
        owner.task = asyncio.create_task(_never())
        await owners.finish("a", owner, False)

        other, _ = owners.admit("b", "request-b", False)
        assert other.engine is second
        delayed.release.set()
        await asyncio.wait_for(asyncio.shield(owner.retirement_task), 1)

    asyncio.run(run())


def test_explicit_resource_free_capability_recovers_capacity_from_hung_close(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(api, "CLOSE_TIMEOUT_SECONDS", 0.02)

    async def run():
        hung = ResourceFreeHungEngine()
        replacement = object()
        engines = iter((hung, replacement))
        owners = Owners(lambda **_: next(engines), tmp_path, limit=1)
        owner, _ = owners.admit("a", "request-a", False)
        owner.task = asyncio.create_task(_never())

        await owners.finish("a", owner, False)
        assert await asyncio.to_thread(hung.close_started.wait, 1)
        assert owner.engine is hung
        assert owner.close_task is not None and not owner.close_task.done()
        other, _ = owners.admit("b", "request-b", False)
        assert other.engine is replacement
        assert hung.close_calls == 1

        owner.retirement_task.cancel()
        await asyncio.gather(owner.retirement_task, return_exceptions=True)

    asyncio.run(run())


def test_background_recheck_recovers_capacity_after_late_physical_proof(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(api, "CLOSE_TIMEOUT_SECONDS", 0.02)

    async def run():
        engine = LateProofHungEngine()
        replacement = object()
        engines = iter((engine, replacement))
        owners = Owners(lambda **_: next(engines), tmp_path, limit=1)
        owner, _ = owners.admit("a", "request-a", False)
        owner.task = asyncio.create_task(_never())

        await owners.finish("a", owner, False)
        assert await asyncio.to_thread(engine.close_started.wait, 1)
        with pytest.raises(api.HTTPException) as full:
            owners.admit("b", "request-b", False)
        assert full.value.status_code == 429

        engine.cleanup_confirmed = True
        for _ in range(20):
            if owner.capacity_released:
                break
            await asyncio.sleep(0.01)
        assert owner.capacity_released
        other, _ = owners.admit("b", "request-b", False)
        assert other.engine is replacement

        assert owner.retirement_task is not None
        owner.retirement_task.cancel()
        await asyncio.gather(owner.retirement_task, return_exceptions=True)

    asyncio.run(run())


def test_close_exception_retains_owner_and_does_not_start_another_close(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(api, "CLOSE_TIMEOUT_SECONDS", 0.02)

    async def run():
        engine = FailingCloseEngine()
        owners = Owners(lambda **_: engine, tmp_path, limit=1)
        owner, _ = owners.admit("a", "request-a", False)

        assert await owners.close_owner("a")
        assert owners.items.get("a") is owner
        assert owner.engine is engine
        assert owner.close_task is not None and not owner.close_task.done()
        assert owner.close_confirmed is False
        with pytest.raises(api.HTTPException) as stuck:
            owners.admit("a", "different-request", False)
        assert stuck.value.status_code == 409
        with pytest.raises(api.HTTPException) as full:
            owners.admit("b", "request-b", False)
        assert full.value.status_code == 429

        assert await owners.close_owner("a")
        await owners.shutdown()
        assert engine.close_calls == 1
        assert owners.items.get("a") is owner

        engine.cleanup_confirmed = True
        assert owner.release_task is not None
        await asyncio.wait_for(asyncio.shield(owner.release_task), 1)
        assert "a" not in owners.items

    asyncio.run(run())


def test_factory_start_failure_does_not_consume_owner_capacity(tmp_path):
    attempts = 0
    replacement = object()

    def factory(**_):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic start failure")
        return replacement

    owners = Owners(factory, tmp_path, limit=1)
    with pytest.raises(api.HTTPException) as unavailable:
        owners.admit("a", "request-a", False)
    assert unavailable.value.status_code == 503
    assert "a" not in owners.items

    other, _ = owners.admit("b", "request-b", False)
    assert other.engine is replacement


def test_prune_and_shutdown_reuse_pending_close_without_early_removal(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(api, "CLOSE_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(api, "OWNER_IDLE_SECONDS", 0.0)

    async def run():
        engine = DelayedResourceEngine()
        owners = Owners(lambda **_: engine, tmp_path)
        owner, _ = owners.admit("a", "request-a", False)
        owner.busy = False

        await owners.prune()
        assert owners.items.get("a") is owner
        assert await asyncio.to_thread(engine.close_started.wait, 1)
        assert engine.close_calls == 1
        await owners.shutdown()
        assert owners.items.get("a") is owner
        assert engine.close_calls == 1

        engine.release.set()
        assert owner.release_task is not None
        await asyncio.wait_for(asyncio.shield(owner.release_task), 1)
        assert "a" not in owners.items

    asyncio.run(run())
