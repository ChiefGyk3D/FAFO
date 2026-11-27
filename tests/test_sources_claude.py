"""Claude source tests: fixtures plus stubs. No real /proc, psutil or git is used."""

from __future__ import annotations

import builtins
import dataclasses
import json
import logging
import os
import shutil
import time
from pathlib import Path

import pytest

from fafo import attribution
from fafo.config import SourceCfg
from fafo.source import Ctx, SourceResult
from fafo.sources import claude
from fafo.sources.claude import ClaudeSource

FIXTURES = Path(__file__).parent / "fixtures" / "claude_sessions"
NOW = 1_700_000_500.0
PEER = "FIXTURE-PEER-TOKEN-4000"
KEY_SECRET = "FIXTURE-KEY-FILE-SECRET-9f8e7d"


@pytest.fixture(autouse=True)
def _fresh_cache():
    attribution.clear_cache()
    yield
    attribution.clear_cache()


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


def by_pid(res: SourceResult) -> dict:
    return {s.pid: s for s in res.sessions}


def test_state_busy_idle_waiting_from_status_field(tmp_path):
    res = source(make_dir(tmp_path), LIVE).collect(ctx())
    got = by_pid(res)
    assert {p: s.state for p, s in got.items()} == {4000: "busy", 4001: "idle", 4002: "waiting"}
    assert got[4002].task == "approval for a tool call"
    assert got[4000].task == "busy"
    assert res.status is None
    assert got[4000].agent == "claude"
    assert got[4000].started == 1_700_000_000.0
    assert got[4000].project.as_of == NOW


def test_unknown_status_maps_to_unknown(tmp_path):
    d = make_dir(tmp_path, ("4000",))
    doc = json.loads((d / "4000.json").read_text())
    doc["status"] = "exploding"
    (d / "4000.json").write_text(json.dumps(doc))
    res = source(d, {4000: "111000"}).collect(ctx())
    assert res.sessions[0].state == "unknown"


def test_pid_reuse_rejected(tmp_path):
    ticks = {**LIVE, 4001: "999999"}  # a different process now owns pid 4001
    res = source(make_dir(tmp_path), ticks).collect(ctx())
    assert sorted(by_pid(res)) == [4000, 4002]


def test_dead_pid_file_ignored(tmp_path):
    ticks = {4000: "111000", 4002: "111002"}  # 4001 has no /proc entry
    res = source(make_dir(tmp_path), ticks).collect(ctx())
    assert sorted(by_pid(res)) == [4000, 4002]


def test_half_written_json_skipped(tmp_path):
    d = make_dir(tmp_path)
    text = (d / "4001.json").read_text()
    (d / "4001.json").write_text(text[: len(text) // 2])
    res = source(d, LIVE).collect(ctx())
    assert sorted(by_pid(res)) == [4000, 4002]


def test_oversized_file_skipped(tmp_path):
    d = make_dir(tmp_path)
    doc = json.loads((d / "4001.json").read_text())
    doc["name"] = "x" * (claude.MAX_FILE_BYTES + 10)
    (d / "4001.json").write_text(json.dumps(doc))
    res = source(d, LIVE).collect(ctx())
    assert sorted(by_pid(res)) == [4000, 4002]


def test_deeply_nested_json_skipped(tmp_path):
    d = make_dir(tmp_path)
    doc = json.loads((d / "4001.json").read_text())
    nested: object = "leaf"
    for _ in range(200):
        nested = [nested]
    doc["waitingFor"] = nested
    (d / "4001.json").write_text(json.dumps(doc))
    res = source(d, LIVE).collect(ctx())
    assert sorted(by_pid(res)) == [4000, 4002]


def test_duplicate_keys_and_pid_filename_mismatch_skipped(tmp_path):
    d = make_dir(tmp_path)
    (d / "4001.json").write_text(
        '{"pid": 4001, "pid": 4001, "sessionId": "dup-1", "procStart": "111001", "status": "idle"}'
    )
    doc = json.loads((d / "4002.json").read_text())
    doc["pid"] = 4000  # claims another pid than its filename
    (d / "4002.json").write_text(json.dumps(doc))
    res = source(d, LIVE).collect(ctx())
    assert sorted(by_pid(res)) == [4000]


def test_non_registry_names_are_ignored_and_symlinks_refused(tmp_path):
    d = make_dir(tmp_path, ("4000",))
    (d / "notes.json").write_text("{}")
    (d / "4000.json.bak").write_text("{}")
    outside = tmp_path / "outside.json"
    shutil.copy(FIXTURES / "4001.json", outside)
    (d / "4001.json").symlink_to(outside)
    res = source(d, LIVE).collect(ctx())
    assert sorted(by_pid(res)) == [4000]


def test_missing_directory_is_absent_not_an_error(tmp_path):
    res = source(tmp_path / "nope", LIVE).collect(ctx())
    assert res.sessions == [] and res.status == "absent"


def test_children_attached_to_owner_session_not_another(tmp_path):
    procs = [
        FakeProc(4000, 1), FakeProc(4001, 1), FakeProc(4002, 1),
        FakeProc(5000, 4000, ["/usr/bin/codex", "exec", "--model", "m1"], cwd="/x"),
        FakeProc(5001, 5000, ["/usr/bin/ollama", "run", "q"], cwd="/x"),
        FakeProc(5100, 4001, ["/bin/sh", "-c", "echo"], cwd="/y"),
        FakeProc(5200, 1, ["/bin/other"], cwd="/z"),  # nobody's child
    ]
    res = source(make_dir(tmp_path), LIVE, procs).collect(ctx())
    owner = {p.pid: p.session_pid for p in res.procs}
    assert owner == {5000: 4000, 5001: 4000, 5100: 4001}
    kinds = {p.pid: p.agent for p in res.procs}
    assert kinds == {5000: "codex", 5001: "ollama", 5100: "other"}
    row = next(p for p in res.procs if p.pid == 5000)
    assert row.ppid == 4000 and row.rss == 2048 and row.cmd.startswith("codex")
    assert "m1" in row.cmd  # --model is shown, so its value is


def test_nested_session_children_belong_to_inner_session(tmp_path):
    procs = [
        FakeProc(4000, 1), FakeProc(4001, 4000), FakeProc(4002, 1),
        FakeProc(6000, 4001, ["/bin/inner-child"], cwd="/y"),
        FakeProc(6001, 4000, ["/bin/outer-child"], cwd="/x"),
    ]
    res = source(make_dir(tmp_path), LIVE, procs).collect(ctx())
    owner = {p.pid: p.session_pid for p in res.procs}
    assert owner == {6000: 4001, 6001: 4000}  # no row for session 4001 itself


def test_dying_child_does_not_fail_the_source(tmp_path):
    class Boom(FakeProc):
        def cmdline(self):
            raise FakeErr("vanished")

    procs = [FakeProc(4000, 1), Boom(5000, 4000, ["x"]), FakeProc(5001, 4000, ["/bin/ok"], cwd="/")]
    res = source(make_dir(tmp_path, ("4000",)), {4000: "111000"}, procs).collect(ctx())
    assert [p.pid for p in res.procs] == [5001]
    assert [s.pid for s in res.sessions] == [4000]


def test_same_cwd_three_sessions_get_three_projects_from_children(tmp_path):
    shared = tmp_path / "src"
    repos = {n: tmp_path / "src" / n for n in ("alpha-repo", "beta-repo", "gamma-repo")}
    shared.mkdir()
    for r in repos.values():
        r.mkdir()
    d = make_dir(tmp_path)
    for n in ("4000", "4001", "4002"):
        doc = json.loads((d / f"{n}.json").read_text())
        doc["cwd"] = str(shared)
        (d / f"{n}.json").write_text(json.dumps(doc))
    procs = [
        FakeProc(4000, 1), FakeProc(4001, 1), FakeProc(4002, 1),
        FakeProc(7000, 4000, ["/bin/a"], cwd=str(repos["alpha-repo"])),
        FakeProc(7001, 4001, ["/bin/b"], cwd=str(repos["beta-repo"])),
        FakeProc(7002, 4002, ["/bin/c"], cwd=str(repos["gamma-repo"])),
    ]
    git = StubGit({str(r): str(r) for r in repos.values()})
    res = source(d, LIVE, procs, git).collect(ctx())
    got = {s.pid: (s.project.value, s.project_rule) for s in res.sessions}
    assert got == {
        4000: ("alpha-repo", "children-git"),
        4001: ("beta-repo", "children-git"),
        4002: ("gamma-repo", "children-git"),
    }


def test_name_and_task_sanitized_and_capped(tmp_path):
    d = make_dir(tmp_path, ("4000",))
    esc = chr(27)
    doc = json.loads((d / "4000.json").read_text())
    doc["name"] = esc + "[31mred" + chr(7) + "n" * 500
    doc["status"] = "waiting"
    doc["waitingFor"] = esc + "]0;title" + chr(7) + "tool " + "w" * 500 + " Bearer sk-abcdefghijklmnop"
    doc["cwd"] = "/srv/" + esc + "[2Jdir"
    (d / "4000.json").write_text(json.dumps(doc))
    s = source(d, {4000: "111000"}).collect(ctx()).sessions[0]
    for text in (s.name, s.task, s.worktree):
        assert esc not in text and chr(7) not in text
    assert 0 < len(s.name) <= claude.MAX_NAME
    assert 0 < len(s.task) <= claude.MAX_TASK
    assert "sk-abcdefghijklmnop" not in s.task


def test_address_is_full_session_id(tmp_path):
    res = source(make_dir(tmp_path), LIVE).collect(ctx())
    got = by_pid(res)
    assert got[4000].address == "aaaaaaaa-0000-4000-8000-000000000001"
    assert got[4000].address == got[4000].session_id
    assert got[4001].name == "beta"


def test_bad_session_id_skipped(tmp_path):
    d = make_dir(tmp_path)
    doc = json.loads((d / "4001.json").read_text())
    doc["sessionId"] = "bad id; rm"
    (d / "4001.json").write_text(json.dumps(doc))
    assert sorted(by_pid(source(d, LIVE).collect(ctx()))) == [4000, 4002]


def _strings(obj, seen=None):
    """Every string reachable from obj, walking dataclasses, dicts, lists and tuples."""
    if isinstance(obj, str):
        yield obj
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            yield f.name
            yield from _strings(getattr(obj, f.name))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from _strings(v)


def test_registry_peer_token_and_key_files_never_read(tmp_path, monkeypatch, caplog):
    d = make_dir(tmp_path)
    key = d / "4000.aabbccddeeff.key"
    key.write_text(KEY_SECRET)
    # a JSON that is not a registry name but carries a secret must not be read either
    (d / "4000.json.key").write_text(KEY_SECRET)

    opened: list[str] = []
    real_open, real_os_open = builtins.open, os.open

    def trap(path):
        opened.append(str(path))
        if str(path).endswith(".key"):
            raise AssertionError(f"key file opened: {path}")

    def spy_open(path, *a, **k):
        trap(path)
        return real_open(path, *a, **k)

    def spy_os_open(path, *a, **k):
        trap(path)
        return real_os_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    with caplog.at_level(logging.DEBUG):
        res = source(d, LIVE).collect(ctx())
    monkeypatch.undo()

    assert len(res.sessions) == 3
    assert opened, "the spy saw no opens, so the trap proves nothing"
    assert not [p for p in opened if p.endswith(".key")]
    blob = "\n".join(_strings(res)) + repr(res) + caplog.text
    for secret in (PEER, KEY_SECRET, "FIXTURE-PEER-TOKEN-4001", "FIXTURE-PEER-TOKEN-4002"):
        assert secret not in blob
    assert "peerToken" not in blob and "messagingSocketPath" not in blob
    assert "/run/fixture" not in blob

    # the parsed record itself no longer carries the secrets
    rec = claude.load_registry_file(d, "4000.json")
    assert rec is not None
    assert "peerToken" not in rec and "messagingSocketPath" not in rec
    assert PEER not in repr(rec)


def test_negative_guard_the_spy_catches_a_key_open(tmp_path, monkeypatch):
    d = make_dir(tmp_path, ("4000",))
    (d / "4000.x.key").write_text(KEY_SECRET)
    real_os_open = os.open
    hit: list[str] = []

    def spy(path, *a, **k):
        if str(path).endswith(".key"):
            hit.append(str(path))
        return real_os_open(path, *a, **k)

    monkeypatch.setattr(os, "open", spy)
    fd = os.open(d / "4000.x.key", os.O_RDONLY)
    os.close(fd)
    assert hit  # the instrument bites when a key file is opened


def test_directory_with_10000_files_is_bounded(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    shutil.copy(FIXTURES / "4000.json", d / "4000.json")
    template = (FIXTURES / "4000.json").read_text()
    for pid in range(10_000, 20_000):
        doc = json.loads(template)
        doc["pid"] = pid
        (d / f"{pid}.json").write_text(json.dumps(doc))
    future = time.time() + 1000
    os.utime(d / "4000.json", (future, future))  # newest
    ticks = {4000: "111000", **{p: "111000" for p in range(10_000, 20_000)}}
    reads: list[str] = []
    real = claude.load_registry_file

    def counting(directory, name):
        reads.append(name)
        return real(directory, name)

    src = source(d, ticks)
    orig = claude.load_registry_file
    claude.load_registry_file = counting
    try:
        res = src.collect(ctx())
    finally:
        claude.load_registry_file = orig
    assert len(reads) == claude.MAX_ENTRIES == 256
    assert "4000.json" in reads  # newest first
    assert len(res.sessions) <= 256


def test_proc_reader_parses_comm_with_parens_and_spaces(tmp_path):
    stat = "123 (we ) ird (name) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 987654 20 21\n"
    assert claude.parse_start_ticks(stat) == "987654"
    assert claude.parse_start_ticks("garbage") is None
    assert claude.parse_start_ticks("1 (a) S 1 2") is None


def test_huge_started_at_is_skipped_and_others_survive(tmp_path):
    d = make_dir(tmp_path)
    doc = json.loads((d / "4001.json").read_text())
    doc["startedAt"] = 10**4000
    (d / "4001.json").write_text(json.dumps(doc))
    doc["startedAt"] = 1e400  # parses to infinity
    (d / "4002.json").write_text(json.dumps(doc).replace("Infinity", "1e400"))
    res = source(d, LIVE).collect(ctx())
    assert sorted(by_pid(res)) == [4000]


def test_started_at_bounds(tmp_path):
    d = make_dir(tmp_path, ("4000",))
    for bad in (-1, True, 4_000_000_000_001, "1700000000000", None):
        doc = json.loads((d / "4000.json").read_text())
        doc["startedAt"] = bad
        (d / "4000.json").write_text(json.dumps(doc))
        assert source(d, {4000: "111000"}).collect(ctx()).sessions == []


def test_an_entry_that_raises_never_kills_the_source(tmp_path):
    class Exploding(StubProcs):
        def start_ticks(self, pid):
            if pid == 4001:
                raise RuntimeError("reader blew up")
            return super().start_ticks(pid)

    procs = [FakeProc(p) for p in LIVE]
    src = ClaudeSource(
        make_dir(tmp_path), show_args=set(), proc_reader=Exploding(LIVE),
        psutil_mod=FakePsutil(procs), git=StubGit({}),
    )
    assert sorted(by_pid(src.collect(ctx()))) == [4000, 4002]


class ChainPsutil(FakePsutil):
    """A lazily generated 100000 process table: one deep chain below session 4000."""

    def __init__(self):
        super().__init__([FakeProc(4000, 1)])
        self.consumed = 0

    def process_iter(self):
        for i in range(100_000):
            self.consumed += 1
            yield FakeProc(4001 + i, 4000 + i, ["/bin/x"], cwd="/")


def test_process_table_is_read_lazily_to_its_cap_and_depth_is_bounded(tmp_path):
    ps = ChainPsutil()
    src = ClaudeSource(
        make_dir(tmp_path, ("4000",)), show_args=set(), proc_reader=StubProcs({4000: "111000"}),
        psutil_mod=ps, git=StubGit({}),
    )
    res = src.collect(ctx())
    assert ps.consumed == claude.MAX_PROCS == 50_000
    assert len(res.procs) == claude.MAX_DEPTH_TREE == 12
    assert [s.pid for s in res.sessions] == [4000]


def test_wide_tree_walk_stops_at_the_per_session_cap(tmp_path):
    calls: list[int] = []

    class Counting(FakeProc):
        def cmdline(self):
            calls.append(self.pid)
            return super().cmdline()

    procs = [FakeProc(4000, 1)] + [Counting(5000 + i, 4000, ["/bin/w"]) for i in range(1000)]
    res = source(make_dir(tmp_path, ("4000",)), {4000: "111000"}, procs).collect(ctx())
    assert len(res.procs) == claude.MAX_CHILDREN == 256
    assert len(calls) == 256  # the walk stopped, rows were not built and then truncated


def test_pid_reuse_between_check_and_usage_read_drops_the_row(tmp_path):
    class Swapping(StubProcs):
        def __init__(self, table):
            super().__init__(table)
            self.calls = 0

        def start_ticks(self, pid):
            self.calls += 1
            return "111000" if self.calls == 1 else "222222"  # replaced after the first read

    src = ClaudeSource(
        make_dir(tmp_path, ("4000",)), show_args=set(), proc_reader=Swapping({}),
        psutil_mod=FakePsutil([FakeProc(4000, 1, rss=123456)]), git=StubGit({}),
    )
    assert src.collect(ctx()).sessions == []


def test_create_time_change_between_reads_drops_the_row(tmp_path):
    class Replaced(FakeProc):
        reads = 0

        def create_time(self):
            Replaced.reads += 1
            return 1_700_000_100.0 + (0 if Replaced.reads == 1 else 5)

    Replaced.reads = 0
    res = source(make_dir(tmp_path, ("4000",)), {4000: "111000"}, [Replaced(4000, 1)])
    assert res.collect(ctx()).sessions == []


# ---- last-known project for idle sessions (FAFO#70) -------------------------------------------


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def _ctx(clock: Clock) -> Ctx:
    return Ctx(cfg=SourceCfg(), clock=clock, log=logging.getLogger("test-claude"))


def _repo_env(tmp_path, names=("4000",), ticks=None, repo="chatty-kathy"):
    d = make_dir(tmp_path, names)
    repo_dir = tmp_path / repo
    repo_dir.mkdir()
    procs = [FakeProc(int(n)) for n in names] + [
        FakeProc(7000 + i, int(n), ["/bin/a"], cwd=str(repo_dir)) for i, n in enumerate(names)
    ]
    git = StubGit({str(repo_dir): str(repo_dir)})
    src = source(d, ticks or {int(n): f"11100{int(n) - 4000}" for n in names}, procs, git)
    return src, repo_dir


def _drop_children(src, names=("4000",)):
    for i, _ in enumerate(names):
        src.ps.table.pop(7000 + i)


def _get(res, pid):
    return by_pid(res)[pid]


def test_idle_session_keeps_last_repo_marked_last(tmp_path):
    clock = Clock()
    src, _ = _repo_env(tmp_path)
    live = _get(src.collect(_ctx(clock)), 4000)
    assert (live.project.value, live.project_source) == ("chatty-kathy", "live")
    _drop_children(src)
    clock.t += 60
    idle = _get(src.collect(_ctx(clock)), 4000)
    assert idle.project.value == "chatty-kathy"
    assert idle.project_source == "last"


def test_never_attributed_falls_back_to_folder(tmp_path):
    src, _ = _repo_env(tmp_path)
    _drop_children(src)
    s = _get(src.collect(_ctx(Clock())), 4000)
    assert s.project_source == "folder"
    assert s.project.value == os.path.basename(json.loads((src.dir / "4000.json").read_text())["cwd"])


def test_different_session_never_inherits_repo(tmp_path):
    clock = Clock()
    src, _ = _repo_env(tmp_path, names=("4000", "4001"), ticks={4000: "111000", 4001: "111001"})
    # only 4000 has a child in the repo; remove 4001's child
    src.ps.table.pop(7001)
    got = by_pid(src.collect(_ctx(clock)))
    assert got[4000].project_source == "live"
    assert got[4001].project_source == "folder"
    assert got[4001].project.value != "chatty-kathy"
    _drop_children(src, ("4000",))
    clock.t += 30
    got = by_pid(src.collect(_ctx(clock)))
    assert got[4000].project.value == "chatty-kathy" and got[4000].project_source == "last"
    assert got[4001].project.value != "chatty-kathy" and got[4001].project_source == "folder"


def test_remembered_name_dropped_after_24h_absent(tmp_path):
    clock = Clock()
    src, _ = _repo_env(tmp_path)
    src.collect(_ctx(clock))
    assert len(src._last_repo) == 1
    # the session id disappears (registry file gone) for just under, then over, 24 h
    reg = src.dir / "4000.json"
    saved = reg.read_text()
    reg.unlink()
    clock.t += claude.LAST_REPO_TTL_S - 1
    src.collect(_ctx(clock))
    assert len(src._last_repo) == 1
    clock.t += 2
    src.collect(_ctx(clock))
    assert len(src._last_repo) == 0
    reg.write_text(saved)
    _drop_children(src)
    s = _get(src.collect(_ctx(clock)), 4000)
    assert s.project_source == "folder"


def test_remembered_cache_is_bounded(tmp_path):
    src, _ = _repo_env(tmp_path)
    clock = Clock()
    for i in range(claude.MAX_LAST_REPOS + 50):
        clock.t += 1
        src._remember(f"sid-{i}", f"repo-{i}", clock.t)
    assert len(src._last_repo) == claude.MAX_LAST_REPOS
    assert "sid-0" not in src._last_repo
    assert f"sid-{claude.MAX_LAST_REPOS + 49}" in src._last_repo
