"""ProcCache and its use by the Claude, Codex and Gemini sources (FAFO#32).

The stubs mimic psutil: cpu_percent(None) is 0.0 on the first call for each OBJECT and the
measured value on later calls. A source that builds a fresh object each cycle therefore never
sees a non-zero reading.
"""

from __future__ import annotations

import logging

import psutil
import pytest
from test_sources_claude import (
    NOW,
    FakeMem,
    FakeProc,
    FakePsutil,
    StubGit,
    StubProcs,
    make_dir,
)

from fafo.config import SourceCfg
from fafo.proccache import MAX_ENTRIES, ProcCache
from fafo.source import Ctx
from fafo.sources.claude import ClaudeSource
from fafo.sources.codex import CodexSource
from fafo.sources.gemini import GeminiSource

MEASURED = 7.5


class CountProc:
    """psutil-like: first cpu_percent call on this object is 0.0, later ones MEASURED."""

    made = 0

    def __init__(self, pid, ppid=1, argv=("x",), created=100.0, cwd="/w", fail=None):
        CountProc.made += 1
        self.pid, self._ppid, self._argv = pid, ppid, list(argv)
        self._created, self._cwd, self._fail = created, cwd, fail
        self.calls = 0

    def ppid(self):
        return self._ppid

    def cmdline(self):
        return list(self._argv)

    def name(self):
        return self._argv[0].rsplit("/", 1)[-1] if self._argv else ""

    def cwd(self):
        return self._cwd

    def create_time(self):
        return self._created

    def memory_info(self):
        return FakeMem(2048)

    def cpu_percent(self, interval=None):
        self.calls += 1
        if self._fail is not None and self.calls > 1:
            raise self._fail(self.pid)
        return 0.0 if self.calls == 1 else MEASURED


class FreshPsutil:
    """Every Process() and process_iter() call builds NEW objects, like a naive source would."""

    Error = psutil.Error

    def __init__(self, specs):
        self.specs = specs  # pid -> kwargs for CountProc

    def Process(self, pid):
        if pid not in self.specs:
            raise psutil.NoSuchProcess(pid)
        return CountProc(pid, **self.specs[pid])

    def process_iter(self):
        return iter([CountProc(pid, **kw) for pid, kw in self.specs.items()])


def ctx():
    return Ctx(cfg=SourceCfg(), clock=lambda: NOW, log=logging.getLogger("t"))


# -- ProcCache itself ------------------------------------------------------------------------
def test_first_cycle_zero_and_primes_then_measured():
    ps = FreshPsutil({5: {}})
    cache = ProcCache(ps)
    assert cache.cpu(5, 100.0) == 0.0
    obj = cache.get(5, 100.0)
    assert obj.calls == 1  # exactly the priming call
    assert cache.cpu(5, 100.0) == MEASURED
    assert cache.get(5, 100.0) is obj


def test_reused_pid_with_new_create_time_gets_a_new_object():
    ps = FreshPsutil({5: {}})
    cache = ProcCache(ps)
    cache.cpu(5, 100.0)
    old = cache.get(5, 100.0)
    assert cache.cpu(5, 200.0) == 0.0  # new identity: primed again, no stale reading
    new = cache.get(5, 200.0)
    assert new is not old and new.calls == 1


def test_end_cycle_prunes_unseen():
    cache = ProcCache(FreshPsutil({1: {}, 2: {}}))
    cache.cpu(1, 100.0)
    cache.cpu(2, 100.0)
    assert len(cache) == 2
    cache.end_cycle({(1, 100.0)})
    assert len(cache) == 1 and cache.get(1, 100.0).calls == 1


def test_bounded_at_4096_oldest_evicted():
    assert MAX_ENTRIES == 4096

    class Many:
        def Process(self, pid):
            return CountProc(pid)

    cache = ProcCache(Many())
    for pid in range(MAX_ENTRIES + 50):
        cache.get(pid, 100.0)
    assert len(cache) == MAX_ENTRIES
    first = cache.get(0, 100.0)  # evicted, so rebuilt and primed once
    assert first.calls == 1
    assert len(cache) == MAX_ENTRIES


@pytest.mark.parametrize("exc", [psutil.NoSuchProcess, psutil.AccessDenied])
def test_cpu_failure_yields_zero_and_drops_entry(exc):
    cache = ProcCache(FreshPsutil({5: {"fail": exc}}))
    cache.cpu(5, 100.0)
    assert cache.cpu(5, 100.0) == 0.0  # second read raises
    assert len(cache) == 0


def test_priming_failure_is_not_cached():
    class Denied:
        def Process(self, pid):
            raise psutil.AccessDenied(pid)

    cache = ProcCache(Denied())
    assert cache.get(5, 100.0) is None and cache.cpu(5, 100.0) == 0.0 and len(cache) == 0


def test_two_caches_do_not_interfere():
    ps = FreshPsutil({5: {}})
    a, b = ProcCache(ps), ProcCache(ps)
    a.cpu(5, 100.0)
    assert a.cpu(5, 100.0) == MEASURED
    assert b.cpu(5, 100.0) == 0.0  # b has never seen it
    assert a.get(5, 100.0) is not b.get(5, 100.0)


# -- Claude source ---------------------------------------------------------------------------
def claude_src(tmp_path, specs, **kw):
    return ClaudeSource(
        make_dir(tmp_path, ("4000",)),
        show_args=set(),
        proc_reader=StubProcs({4000: "111000"}),
        psutil_mod=FreshPsutil(specs),
        git=StubGit({}),
        **kw,
    )


CLAUDE_SPECS = {
    4000: {"ppid": 1, "argv": ["claude"], "created": 1_700_000_100.0},
    5000: {"ppid": 4000, "argv": ["/usr/bin/codex"], "created": 1_700_000_200.0},
}


def test_claude_session_and_child_cpu_nonzero_on_second_cycle(tmp_path):
    src = claude_src(tmp_path, CLAUDE_SPECS)
    first = src.collect(ctx())
    assert first.sessions[0].cpu == 0.0 and first.procs[0].cpu == 0.0
    second = src.collect(ctx())
    assert second.sessions[0].cpu == MEASURED
    assert second.procs[0].cpu == MEASURED


def test_claude_exited_child_is_pruned(tmp_path):
    ps_specs = dict(CLAUDE_SPECS)
    src = claude_src(tmp_path, ps_specs)
    src.collect(ctx())
    assert len(src.cache) == 2
    del src.ps.specs[5000]
    src.collect(ctx())
    assert len(src.cache) == 1


def test_claude_child_raising_on_cpu_gets_zero_and_source_survives(tmp_path):
    specs = {**CLAUDE_SPECS, 5000: {**CLAUDE_SPECS[5000], "fail": psutil.AccessDenied}}
    src = claude_src(tmp_path, specs)
    src.collect(ctx())
    res = src.collect(ctx())
    assert res.sessions[0].cpu == MEASURED
    assert [p.cpu for p in res.procs] == [0.0]


def test_claude_sources_with_separate_caches_do_not_share(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = claude_src(tmp_path / "a", CLAUDE_SPECS)
    b = claude_src(tmp_path / "b", CLAUDE_SPECS)
    a.collect(ctx())
    assert a.collect(ctx()).sessions[0].cpu == MEASURED
    assert b.collect(ctx()).sessions[0].cpu == 0.0


def test_claude_backward_compatible_with_existing_stub_psutil(tmp_path):
    src = ClaudeSource(
        make_dir(tmp_path, ("4000",)),
        proc_reader=StubProcs({4000: "111000"}),
        psutil_mod=FakePsutil([FakeProc(4000, 1)]),
        git=StubGit({}),
    )
    assert len(src.collect(ctx()).sessions) == 1


# -- Codex and Gemini ------------------------------------------------------------------------
def fresh_lister(specs):
    ps = FreshPsutil(specs)
    return ps, (lambda: ps.process_iter())


@pytest.mark.parametrize(
    ("cls", "argv"),
    [(CodexSource, ["/usr/bin/codex", "exec"]), (GeminiSource, ["/usr/bin/gemini"])],
)
def test_codex_gemini_cpu_nonzero_on_second_cycle(cls, argv):
    ps, lister = fresh_lister({7: {"argv": argv}})
    src = cls(lister=lister, cache=ProcCache(ps))
    assert src.collect(ctx()).procs[0].cpu == 0.0
    assert src.collect(ctx()).procs[0].cpu == MEASURED


def test_codex_prunes_and_survives_cpu_failure():
    ps, lister = fresh_lister({7: {"argv": ["/usr/bin/codex"], "fail": psutil.NoSuchProcess}})
    cache = ProcCache(ps)
    src = CodexSource(lister=lister, cache=cache)
    src.collect(ctx())
    res = src.collect(ctx())
    assert [p.cpu for p in res.procs] == [0.0] and len(cache) == 0
    ps.specs.clear()
    src.collect(ctx())
    assert len(cache) == 0


def test_codex_two_sources_with_own_caches_do_not_interfere():
    ps, lister = fresh_lister({7: {"argv": ["/usr/bin/codex"]}})
    a = CodexSource(lister=lister, cache=ProcCache(ps))
    b = CodexSource(lister=lister, cache=ProcCache(ps))
    a.collect(ctx())
    assert a.collect(ctx()).procs[0].cpu == MEASURED
    assert b.collect(ctx()).procs[0].cpu == 0.0
