"""Shared fixture: record the order of group kills and reaps of every Popen in a test."""

import os
import subprocess

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
