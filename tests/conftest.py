"""Shared fixture: record the order of group kills and reaps of every Popen in a test."""

import os
import subprocess
import time

import pytest


class GroupKillSpy:
    def __init__(self):
        self.events = []  # ("kill", pid) or ("wait", pid)
        self.popens = []
        self.reaped_when_killed = []  # pids killed after their Popen had a returncode

    def kills(self):
        return [e for e in self.events if e[0] == "kill"]


@pytest.fixture
def group_kill_spy(monkeypatch):
    spy = GroupKillSpy()
    real_killpg = os.killpg
    real_wait = subprocess.Popen.wait
    real_init = subprocess.Popen.__init__

    def init(self, *a, **kw):
        real_init(self, *a, **kw)
        spy.popens.append(self)

    def wait(self, timeout=None):
        spy.events.append(("wait", self.pid))
        return real_wait(self, timeout)

    def killpg(pid, sig):
        spy.events.append(("kill", pid))
        for p in spy.popens:
            if p.pid == pid and p.returncode is not None:
                spy.reaped_when_killed.append(pid)
        return real_killpg(pid, sig)

    monkeypatch.setattr(subprocess.Popen, "__init__", init)
    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    monkeypatch.setattr(os, "killpg", killpg)
    return spy


def pid_alive(pid: int) -> bool:
    """True for a running process; a zombie (state Z) or a missing pid reads as gone.

    os.kill(pid, 0) succeeds for a zombie, so the state is read from /proc/<pid>/stat. The
    state follows the last ')' because the command name may itself contain ')' (proc(5)).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def wait_gone(pids, limit: float = 5.0) -> bool:
    """Poll up to `limit` seconds (short sleeps) until no pid in `pids` is alive.

    SIGKILL delivery and the zombie hand-off are asynchronous, so a kill or a runner return
    never makes a pid read gone instantly: assert through this helper, not on one probe.
    """
    end = time.monotonic() + limit
    while True:
        if not any(pid_alive(p) for p in pids):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)


def deadline_bound(deadline: float) -> float:
    """Upper bound for a measured runner call: the deadline plus 20 percent plus 2 s of slack.

    Used only on the elapsed time of the runner call itself, never on child start-up.
    """
    return deadline * 1.2 + 2.0


def wait_group_gone(pgid: int, limit: float = 5.0) -> bool:
    """Poll until killpg(pgid, 0) reports no member left (a zombie member still counts)."""
    end = time.monotonic() + limit
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)


def wait_for_file(path, timeout: float = 10.0) -> str:
    """Poll up to `timeout` seconds for a non-empty, newline-terminated file; return its text.

    A child records its pid(s) with one `echo`, so a complete line means the write finished.
    Fails the test with a clear message when the file never appears (the child never started).
    """
    end = time.monotonic() + timeout
    while True:
        try:
            with open(path) as f:
                text = f.read()
        except FileNotFoundError:
            text = ""
        if text.endswith("\n"):
            return text
        if time.monotonic() >= end:
            pytest.fail(f"{path} not written within {timeout}s (child never recorded its pid)")
        time.sleep(0.02)
