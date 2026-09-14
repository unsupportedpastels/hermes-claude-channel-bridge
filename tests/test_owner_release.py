import asyncio
import threading

import pytest

from claude_native_bridge.api import Owners


def test_explicit_owner_release_survives_cancellation(tmp_path):
    async def run():
        started, release = threading.Event(), threading.Event()
        class Engine:
            calls = 0
            def close(self):
                self.calls += 1
                started.set()
                release.wait(2)
        engine = Engine()
        owners = Owners(lambda **kw: engine, tmp_path)
        owner, _ = owners.admit('a', 'request', False)
        generation = asyncio.create_task(asyncio.Event().wait())
        owner.task = generation
        waiter = asyncio.create_task(owners.close_owner('a'))
        assert await asyncio.to_thread(started.wait, 1)
        waiter.cancel()
        await asyncio.sleep(0)
        waiter.cancel()
        assert owners.items.get('a') is owner
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert generation.done()
        assert 'a' not in owners.items
        assert engine.calls == 1
    asyncio.run(run())
