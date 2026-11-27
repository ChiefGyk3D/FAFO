"""Tests of run_when_ready itself: fake scenarios, no processes."""

import os

import pytest
from conftest import READINESS_LOG, Ran, run_when_ready


def make_scenario(tmp_path, ready_on, seen):
    """A scenario that writes its pidfile only on attempt number `ready_on` (1-based)."""

    def scenario(deadline):
        seen.append(deadline)
        pf = tmp_path / f"pid{len(seen)}"
        if ready_on is not None and len(seen) == ready_on:
            pf.write_text("123\n")
        return Ran(("result", deadline), pf)

    return scenario


def test_ready_first_attempt_runs_once(tmp_path):
    seen = []
    r = run_when_ready(make_scenario(tmp_path, 1, seen))
    assert seen == [3.0] and r.deadline == 3.0 and r.attempts == 1
    assert r.pids == [123] and r.result == ("result", 3.0)


def test_not_ready_then_ready_retries_with_longer_deadline(tmp_path, monkeypatch):
    # A pinned quiet load average: if the retry is broken this must FAIL, never skip.
    monkeypatch.setattr(os, "getloadavg", lambda: (1.0, 1.0, 1.0))
    seen = []
    r = run_when_ready(make_scenario(tmp_path, 2, seen))
    assert seen == [3.0, 6.0] and r.deadline == 6.0 and r.attempts == 2
    assert not (tmp_path / "pid1").exists()  # the not-ready attempt's pidfile was removed
    assert READINESS_LOG[-2:] == ["not_ready", "ready"]


def test_never_ready_fails_on_a_quiet_machine(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getloadavg", lambda: (1.0, 1.0, 1.0))
    seen = []
    with pytest.raises(pytest.fail.Exception, match="never started"):
        run_when_ready(make_scenario(tmp_path, None, seen))
    assert seen == [3.0, 6.0, 12.0]


def test_never_ready_fails_on_a_moderately_loaded_machine(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getloadavg", lambda: (10.0, 1.0, 1.0))
    with pytest.raises(pytest.fail.Exception):
        run_when_ready(make_scenario(tmp_path, None, []))


def test_never_ready_still_fails_on_an_overloaded_machine(tmp_path, monkeypatch):
    """High load never turns a regression into a skip: it fails with the full diagnostic."""
    monkeypatch.setattr(os, "getloadavg", lambda: (40.0, 1.0, 1.0))
    seen = []
    try:
        with pytest.raises(
            pytest.fail.Exception, match=r"3 attempts.*3\.0, 6\.0, 12\.0.*load average 40\.0"
        ):
            run_when_ready(make_scenario(tmp_path, None, seen))
    except pytest.skip.Exception:
        pytest.fail("a never-ready child was skipped under load instead of failing")
    assert seen == [3.0, 6.0, 12.0]


def test_ready_child_that_survives_still_fails_the_assertions(tmp_path):
    """Readiness never hides a survivor: the caller's own assertion runs and fails."""
    import subprocess

    pids = []

    def scenario(deadline):
        pf = tmp_path / "pid"
        proc = subprocess.Popen(["/bin/sh", "-c", 'echo $$ > "$1"; exec sleep 30', "sh", str(pf)])
        pids.append(proc.pid)
        # no kill: this fake runner leaves the child alive
        return Ran(None, pf)

    try:
        r = run_when_ready(scenario)
        from conftest import wait_gone

        with pytest.raises(AssertionError):
            assert wait_gone(r.pids, limit=0.3), "child survived the kill"
    finally:
        import signal

        for pid in pids:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
