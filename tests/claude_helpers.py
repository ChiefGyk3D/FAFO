"""Shared stubs and builders for the Claude source tests (imported like conftest helpers)."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from fafo.config import SourceCfg
from fafo.source import Ctx
from fafo.sources.claude import ClaudeSource

FIXTURES = Path(__file__).parent / "fixtures" / "claude_sessions"
NOW = 1_700_000_500.0


class StubProcs:
    """ProcReader stub: pid -> start ticks string."""

    def __init__(self, table: dict[int, str]):
        self.table = table

    def start_ticks(self, pid: int) -> str | None:
        return self.table.get(pid)


class FakeErr(Exception):
    pass


class FakeMem:
    def __init__(self, rss: int):
        self.rss = rss


class FakeProc:
    def __init__(self, pid, ppid=0, argv=(), cwd=None, rss=2048, cpu=1.5, created=1_700_000_100.0):
        self._pid, self._ppid, self._argv, self._cwd = pid, ppid, list(argv), cwd
        self._rss, self._cpu, self._created = rss, cpu, created
        self.table: dict[int, FakeProc] = {}

    @property
    def pid(self):
        return self._pid

    def ppid(self):
        return self._ppid

    def cmdline(self):
        return self._argv

    def name(self):
        return os.path.basename(self._argv[0]) if self._argv else ""

    def cwd(self):
        if self._cwd is None:
            raise FakeErr("no cwd")
        return self._cwd

    def cpu_percent(self, interval=None):
        return self._cpu

    def memory_info(self):
        return FakeMem(self._rss)

    def create_time(self):
        return self._created

    def children(self, recursive=False):
        out: list[FakeProc] = []
        todo = [self._pid]
        while todo:
            cur = todo.pop(0)
            for p in self.table.values():
                if p._ppid == cur:
                    out.append(p)
                    todo.append(p._pid)
        return out


class FakePsutil:
    Error = FakeErr

    def __init__(self, procs: list[FakeProc]):
        self.table = {p.pid: p for p in procs}
        for p in procs:
            p.table = self.table

    def process_iter(self):
        return iter(list(self.table.values()))

    def Process(self, pid):
        if pid not in self.table:
            raise FakeErr("gone")
        return self.table[pid]


class StubGit:
    """Maps a directory to a toplevel; the repo name is the toplevel basename."""

    def __init__(self, tops: dict[str, str]):
        self.tops = tops

    def run(self, args, cwd, timeout):
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return self.tops.get(cwd)
        return None


def ctx() -> Ctx:
    return Ctx(cfg=SourceCfg(), clock=lambda: NOW, log=logging.getLogger("test-claude"))


def make_dir(tmp_path: Path, names=("4000", "4001", "4002")) -> Path:
    d = tmp_path / "sessions"
    d.mkdir()
    for n in names:
        shutil.copy(FIXTURES / f"{n}.json", d / f"{n}.json")
    return d


def source(d: Path, ticks: dict[int, str], procs: list[FakeProc] | None = None, git=None):
    if procs is None:
        procs = [FakeProc(p) for p in ticks]
    return ClaudeSource(
        d,
        show_args={"exec", "--model"},
        proc_reader=StubProcs(ticks),
        psutil_mod=FakePsutil(procs),
        git=git or StubGit({}),
    )


LIVE = {4000: "111000", 4001: "111001", 4002: "111002"}
