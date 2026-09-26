"""Offline frozen-CLI detection tests; no native process or model calls."""

import subprocess

import pytest

from claude_native_bridge.settings import NativeBridgeError, Settings

from test_deadlines import FakeClock, make_session

POLLS = 2000
# A dead CLI is noticed at the first probe after it exits (5 s interval).
STALL_BOUND = 7


def idle_scripts():
    return {
        "/advance": [(0.01, {"accepted": True})],
        "/response": [(0.5, {"response": None})] * POLLS,
        "/status": [(0.0, {"failed": None})] * POLLS,
    }


def with_pane(session, monkeypatch, screen, alive=lambda: True):
    def tmux(*args, check=True):
        if args[0] == "has-session":
            return subprocess.CompletedProcess(args, 0 if alive() else 1, "", "")
        if args[0] == "capture-pane":
            return subprocess.CompletedProcess(args, 0, screen(), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(session, "_tmux", tmux)


def stall_session(tmp_path, clock, monkeypatch, request_timeout=600, stall=30):
    session, http = make_session(
        tmp_path, clock, idle_scripts(), monkeypatch, timeout=request_timeout
    )
    session.settings = Settings(
        request_timeout=request_timeout, stall_timeout=stall, retain_diagnostics=True
    )
    return session, http


def test_unchanged_pane_fails_the_request_after_the_stall_period(tmp_path, monkeypatch):
    clock = FakeClock()
    session, _ = stall_session(tmp_path, clock, monkeypatch)
    with_pane(session, monkeypatch, lambda: "frozen")

    with pytest.raises(NativeBridgeError, match="stopped updating"):
        session.exchange("frame", "r")

    # First probe at ~5 s records the pane; it must stay unchanged for 30 s.
    assert 10 + 35 <= clock.now <= 10 + 42
    assert session.closed


def test_repainting_pane_is_bounded_only_by_the_request_timeout(tmp_path, monkeypatch):
    clock = FakeClock()
    session, _ = stall_session(tmp_path, clock, monkeypatch, request_timeout=120)
    with_pane(session, monkeypatch, lambda: "thinking %.1f" % clock.now)

    with pytest.raises(TimeoutError, match="timed out"):
        session.exchange("frame", "r")

    assert clock.now == pytest.approx(130, abs=1)


def test_channel_wakes_count_as_progress(tmp_path, monkeypatch):
    clock = FakeClock()
    session, http = stall_session(tmp_path, clock, monkeypatch, request_timeout=120)
    wakes = iter(range(1, POLLS))
    http.scripts["/response"] = [
        (0.5, {"response": None, "wake": {"generation": next(wakes), "event": "Usage"}})
        if i % 20 == 0
        else (0.5, {"response": None})
        for i in range(POLLS)
    ]
    with_pane(session, monkeypatch, lambda: "frozen")

    with pytest.raises(TimeoutError, match="timed out"):
        session.exchange("frame", "r")


def test_unobservable_pane_is_not_a_stall(tmp_path, monkeypatch):
    clock = FakeClock()
    session, _ = stall_session(tmp_path, clock, monkeypatch, request_timeout=120)
    monkeypatch.setattr(session, "health", lambda: True)
    monkeypatch.setattr(session, "_screen", lambda: None)

    with pytest.raises(TimeoutError, match="timed out"):
        session.exchange("frame", "r")


def test_dead_cli_is_reported_as_a_lost_session_within_one_probe(tmp_path, monkeypatch):
    clock = FakeClock()
    session, _ = stall_session(tmp_path, clock, monkeypatch)
    with_pane(session, monkeypatch, lambda: "x", alive=lambda: clock.now < 12)

    with pytest.raises(NativeBridgeError, match="lost"):
        session.exchange("frame", "r")

    assert clock.now <= 10 + STALL_BOUND
