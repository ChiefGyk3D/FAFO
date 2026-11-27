"""Tests for fafo.procutil: kill-then-reap, never signal a reaped process."""

import os
import signal
import subprocess
import sys
import time

import pytest

from fafo import procutil


def spawn(code):
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


@pytest.fixture
def killpg_calls(monkeypatch):
    calls = []
    real = os.killpg

    def spy(pid, sig):
        calls.append((pid, sig))
        return real(pid, sig)

    monkeypatch.setattr(os, "killpg", spy)
    return calls


def test_reaped_process_gets_no_signal(killpg_calls):
    proc = spawn("pass")
    proc.wait(timeout=5)
    assert proc.returncode is not None
    procutil.kill_group_if_running(proc)
    procutil.kill_and_reap(proc)
    assert killpg_calls == []


def test_running_process_is_killed_with_sigkill(killpg_calls):
    proc = spawn("import time; time.sleep(30)")
    try:
        procutil.kill_and_reap(proc)
        assert killpg_calls == [(proc.pid, signal.SIGKILL)]
        assert proc.returncode == -signal.SIGKILL
    finally:
        if proc.returncode is None:
            proc.kill()
            proc.wait()


def test_second_call_after_reap_is_a_noop(killpg_calls):
    proc = spawn("import time; time.sleep(30)")
    procutil.kill_and_reap(proc)
    procutil.kill_and_reap(proc)
    assert len(killpg_calls) == 1


def test_exited_unreaped_child_still_gets_group_kill(killpg_calls):
    """A zombie leader is unreaped (returncode None): its group is still ours to kill."""
    proc = spawn("pass")
    assert procutil.wait_exited_unreaped(proc, 5.0) is True
    assert proc.returncode is None  # waiting did not reap
    procutil.kill_and_reap(proc)
    assert len(killpg_calls) == 1
    assert proc.returncode == 0


def test_wait_exited_unreaped_times_out_on_running_child():
    proc = spawn("import time; time.sleep(30)")
    try:
        t0 = time.monotonic()
        assert procutil.wait_exited_unreaped(proc, 0.2) is False
        assert time.monotonic() - t0 < 2
        assert proc.returncode is None
    finally:
        procutil.kill_and_reap(proc)


def test_missing_group_and_permission_errors_are_tolerated(monkeypatch):
    proc = spawn("import time; time.sleep(30)")
    try:
        for exc in (ProcessLookupError, PermissionError):
            def boom(pid, sig, exc=exc):
                raise exc()

            monkeypatch.setattr(os, "killpg", boom)
            procutil.kill_group_if_running(proc)  # must not raise
        monkeypatch.undo()
    finally:
        procutil.kill_and_reap(proc)


def test_waitid_childprocesserror_means_reaped_no_signal(killpg_calls, monkeypatch):
    proc = spawn("import time; time.sleep(30)")
    try:
        def gone(*a, **kw):
            raise ChildProcessError()

        monkeypatch.setattr(os, "waitid", gone)
        assert procutil.wait_exited_unreaped(proc, 1.0) is True
        procutil.kill_and_reap(proc)
        procutil.kill_group_if_running(proc)
        assert killpg_calls == []
        assert proc.returncode is not None
    finally:
        monkeypatch.undo()
        proc.kill()
        proc.wait()


def test_externally_reaped_child_gets_no_signal(killpg_calls):
    proc = spawn("pass")
    os.waitpid(proc.pid, 0)  # someone else reaps; Popen.returncode stays None
    assert proc.returncode is None
    procutil.kill_and_reap(proc)
    assert killpg_calls == []
    assert proc.returncode is not None


def test_externally_reaped_child_after_wait_helper_gets_no_signal(killpg_calls):
    proc = spawn("pass")
    os.waitpid(proc.pid, 0)
    assert procutil.wait_exited_unreaped(proc, 1.0) is True
    procutil.kill_and_reap(proc)
    assert killpg_calls == []
