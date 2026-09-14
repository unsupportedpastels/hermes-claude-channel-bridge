import asyncio
import threading
import time
from typing import ClassVar

import pytest

from claude_native_bridge import api
from claude_native_bridge.api import Owners
from claude_native_bridge.client import NativeBridgeClient
from claude_native_bridge.settings import NativeBridgeError, Settings


class BlockingEngine:
    def __init__(self, release=None):
        self.release = release or threading.Event()
        self.close_started = threading.Event()
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self.close_started.set()
        self.release.wait(2)


async def _never():
    await asyncio.Event().wait()


def test_owner_retirement_is_retained_and_survives_repeated_cancellation(tmp_path):
    async def run():
        release = threading.Event()
        engine = BlockingEngine(release)
        owners = Owners(lambda **_: engine, tmp_path)
        owner, cached = owners.admit("owner-a", "request-a", False)
        assert not cached
        owner.task = asyncio.create_task(_never())

        waiter = asyncio.create_task(owners.finish("owner-a", owner, False))
        assert await asyncio.to_thread(engine.close_started.wait, 1)
        waiter.cancel()
        await asyncio.sleep(0)
        waiter.cancel()

        assert owner.cleanup_started
        assert not owner.cleanup_completed
        assert owner.retirement_task is not None
        assert not owner.retirement_task.cancelled()
        assert not waiter.done()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await asyncio.wait_for(asyncio.shield(owner.retirement_task), 1)

        assert owner.cleanup_completed
        retirement = owner.retirement_task
        assert retirement.done()
        assert engine.close_calls == 1
        assert owner.failed
        assert not owner.busy
        assert owner.task is None

        with pytest.raises(api.HTTPException) as refused:
            owners.admit("owner-a", "request-a", False)
        assert refused.value.status_code == 502
        assert owner.retirement_task is retirement

    asyncio.run(run())


def test_slow_owner_retirement_does_not_block_another_owner(tmp_path):
    async def run():
        release = threading.Event()
        engines = {"owner-a": BlockingEngine(release), "owner-b": BlockingEngine()}
        next_owner = iter(("owner-a", "owner-b"))
        owners = Owners(lambda **_: engines[next(next_owner)], tmp_path)
        owner_a, _ = owners.admit("owner-a", "request-a", False)
        owner_a.task = asyncio.create_task(_never())
        retiring = asyncio.create_task(owners.finish("owner-a", owner_a, False))
        assert await asyncio.to_thread(engines["owner-a"].close_started.wait, 1)

        owner_b, _ = owners.admit("owner-b", "request-b", False)
        started = asyncio.get_running_loop().time()
        await owners.finish("owner-b", owner_b, True)
        assert asyncio.get_running_loop().time() - started < 0.1
        assert owner_b.cleanup_completed
        assert not owner_b.failed
        assert not owner_b.busy
        assert not retiring.done()

        release.set()
        await asyncio.wait_for(retiring, 1)

    asyncio.run(run())


def test_owner_retirement_has_a_bounded_cleanup_allowance(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "CLOSE_TIMEOUT_SECONDS", 0.03)

    async def run():
        engine = BlockingEngine()
        owners = Owners(lambda **_: engine, tmp_path)
        owner, _ = owners.admit("owner-a", "request-a", False)
        owner.task = asyncio.create_task(_never())

        started = asyncio.get_running_loop().time()
        await owners.finish("owner-a", owner, False)
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 0.2
        assert owner.cleanup_started
        assert not owner.cleanup_completed
        retirement = owner.retirement_task
        assert retirement is not None and not retirement.done()
        assert engine.close_calls == 1
        engine.release.set()
        await asyncio.wait_for(asyncio.shield(retirement), 1)
        assert owner.cleanup_completed

    asyncio.run(run())


class SlowClosingNative:
    instances: ClassVar[list] = []
    exchange_started: ClassVar[threading.Event] = threading.Event()

    def __init__(self, settings, home, model, effort, **kwargs):
        self.closed = False
        self.close_calls = 0
        self.runtime = None
        self.last_usage = None
        self.last_response_source = "respond"
        self.last_text = ""
        self.__class__.instances.append(self)

    def start(self):
        return self

    def exchange(self, content, request_id, **kwargs):
        self.__class__.exchange_started.set()
        while not self.closed:
            time.sleep(0.001)
        raise NativeBridgeError("cancelled")

    def close(self):
        self.close_calls += 1
        time.sleep(0.15)
        self.closed = True


def test_async_client_cancellation_offloads_and_retains_single_close(tmp_path):
    async def run():
        SlowClosingNative.instances = []
        SlowClosingNative.exchange_started = threading.Event()
        client = NativeBridgeClient(
            hermes_home=tmp_path,
            settings=Settings(development_channels_accepted=True),
            native_factory=SlowClosingNative,
        )
        request = {
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "hello"}],
            "extra_body": {"hermes_session_id": "owner-a"},
        }
        task = asyncio.create_task(client.chat.completions.create(**request))
        assert await asyncio.to_thread(SlowClosingNative.exchange_started.wait, 1)

        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        tick_started = asyncio.get_running_loop().time()
        await asyncio.sleep(0.02)
        assert asyncio.get_running_loop().time() - tick_started < 0.08

        with pytest.raises(asyncio.CancelledError):
            await task
        retirement = client._async_close_task
        assert retirement is not None
        await asyncio.wait_for(asyncio.shield(retirement), 1)
        assert SlowClosingNative.instances[0].close_calls == 1
        assert client.is_closed

    asyncio.run(run())
