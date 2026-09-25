import asyncio
import os
import subprocess
import sys

import httpx
import psutil
import pytest

from claude_native_bridge.api import create_app
from claude_native_bridge.service_lifecycle import (
    ServiceActivity,
    parse_identity,
    process_alive,
)

TOKEN = "t" * 40
AUTH = {"Authorization": "Bearer " + TOKEN}


def _activity(idle=60):
    now = [0.0]
    alive = {}
    activity = ServiceActivity(
        idle, clock=lambda: now[0], alive=lambda pid, created: alive.get((pid, created), False)
    )
    return activity, now, alive


def test_retires_only_after_idle_period_with_no_open_client():
    activity, now, alive = _activity()
    activity.note_client("100:5.0", "owner-a")
    alive[(100, 5.0)] = True
    now[0] = 59
    assert not activity.should_retire(False)
    now[0] = 61
    assert not activity.should_retire(False), "an open client keeps the service"
    alive[(100, 5.0)] = False
    assert activity.should_retire(False)


def test_live_process_without_open_client_does_not_pin_the_service():
    activity, now, alive = _activity()
    alive[(100, 5.0)] = True
    activity.note_client("100:5.0")  # health probe with no client
    now[0] = 61
    assert activity.should_retire(False)


def test_closing_the_last_client_releases_its_process():
    activity, now, alive = _activity()
    alive[(100, 5.0)] = True
    activity.note_client("100:5.0", "owner-a")
    activity.note_client("100:5.0", "owner-b")
    now[0] = 61
    activity.release_client("100:5.0", "owner-a")
    assert not activity.should_retire(False), "owner-b is still open"
    activity.release_client("100:5.0", "owner-b")
    assert activity.should_retire(False)


def test_release_from_a_different_identity_is_ignored_and_reuse_resets():
    activity, now, alive = _activity()
    alive[(100, 5.0)] = alive[(100, 9.0)] = True
    activity.note_client("100:5.0", "owner-a")
    activity.release_client("100:9.0", "owner-a")
    assert activity.open == {100: {"owner-a"}}
    activity.note_client("100:9.0")  # reused PID: the old process's clients are gone
    now[0] = 61
    assert activity.should_retire(False)


def test_in_flight_requests_and_busy_owners_block_retirement():
    activity, now, _ = _activity()
    activity.enter()
    now[0] = 1000
    assert not activity.should_retire(False)
    activity.exit()
    now[0] = 2000
    assert not activity.should_retire(True)
    assert activity.should_retire(False)


def test_zero_idle_period_never_retires():
    activity, now, _ = _activity(idle=0)
    now[0] = 10**6
    assert not activity.should_retire(False)


@pytest.mark.parametrize(
    "value", [None, "", "abc", "12", "12:", "-1:5.0", "0:5.0", "1:nan", "1:inf", "1:-3", "9" * 70]
)
def test_malformed_identities_are_ignored(value):
    assert parse_identity(value) is None
    activity, _, _ = _activity()
    activity.note_client(value)
    assert activity.clients == {}


def test_process_identity_detects_exit_and_pid_reuse():
    me = psutil.Process()
    assert process_alive(me.pid, me.create_time())
    assert not process_alive(me.pid, me.create_time() - 100), "reused PID is not the client"
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    created = psutil.Process(child.pid).create_time()
    child.wait()
    assert not process_alive(child.pid, created)


def _app(idle_exit=None, idle_seconds=0):
    return create_app(
        TOKEN,
        "/nonexistent-home",
        engine_factory=lambda **kwargs: None,
        idle_exit=idle_exit,
        idle_seconds=idle_seconds,
    )


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge")


def test_only_authenticated_requests_register_a_client_process():
    app = _app()
    identity = f"{os.getpid()}:{psutil.Process().create_time()!r}"

    async def run():
        async with await _client(app) as client:
            denied = await client.get("/health", headers={"X-Hermes-Bridge-Process": identity})
            assert denied.status_code == 401
            assert app.state.activity.clients == {}
            ok = await client.get(
                "/health", headers={**AUTH, "X-Hermes-Bridge-Process": identity}
            )
            assert ok.status_code == 200

    asyncio.run(run())
    assert os.getpid() in app.state.activity.clients


def test_client_is_open_from_creation_probe_until_owner_close():
    app = _app()
    process = {"X-Hermes-Bridge-Process": f"{os.getpid()}:{psutil.Process().create_time()!r}"}
    owner = {"X-Hermes-Bridge-Client": "owner-a"}

    async def run():
        async with await _client(app) as client:
            await client.get("/health", headers={**AUTH, **process, **owner})
            assert app.state.activity.open == {os.getpid(): {"owner-a"}}
            closed = await client.post("/v1/owner/close", headers={**AUTH, **process, **owner})
            assert closed.status_code == 200
            assert app.state.activity.open == {}

    asyncio.run(run())


def test_shutdown_drains_refuses_new_work_then_exits():
    exits = []
    app = _app(idle_exit=lambda: exits.append(True), idle_seconds=300)

    async def run():
        async with await _client(app) as client:
            accepted = await client.post("/v1/service/shutdown", headers=AUTH)
            assert accepted.status_code == 202
            health = await client.get("/health", headers=AUTH)
            assert health.status_code == 503
            assert health.json() == {"service": "claude-native-bridge", "status": "draining"}
            refused = await client.get("/v1/models", headers=AUTH)
            assert refused.status_code == 503
            await asyncio.wait_for(app.state.drain_task, 5)

    asyncio.run(run())
    assert exits == [True]


def test_service_without_a_host_exit_refuses_remote_shutdown():
    app = _app()

    async def run():
        async with await _client(app) as client:
            response = await client.post("/v1/service/shutdown", headers=AUTH)
            assert response.status_code == 409
            assert (await client.get("/health", headers=AUTH)).status_code == 200

    asyncio.run(run())
