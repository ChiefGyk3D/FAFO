"""Codex you-are-here: the SessionStart hook script and the bounded reader of its files."""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from fafo.config import SourceCfg
from fafo.model import SNAPSHOT_VERSION, Snapshot
from fafo.render import commands
from fafo.render.view import build_views
from fafo.source import Ctx
from fafo.sources import codex as codex_mod
from fafo.sources.codex import CodexSource, read_here_dir

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "examples" / "codex" / "fafo-session-hook.sh"
SID = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001"
UID = os.geteuid()


# ---- the hook script -------------------------------------------------------------------------


def run_hook(tmp_path, stdin: bytes, *, arg: str | None = None):
    state = tmp_path / "state"
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home")}
    env["XDG_RUNTIME_DIR"] = str(state)
    cmd = ["bash", str(HOOK)] + ([arg] if arg else [])
    r = subprocess.run(cmd, input=stdin, capture_output=True, env=env, timeout=30, check=False)
    return r, state / "fafo" / "codex-here"


def test_hook_writes_session_file(tmp_path):
    payload = json.dumps(
        {"session_id": SID, "transcript_path": "/nonexistent/secret.jsonl", "cwd": "/x"}
    ).encode()
    r, here = run_hook(tmp_path, payload)
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")
    files = list(here.iterdir())
    assert [f.name for f in files] == [f"{os.getpid()}.json"]
    f = files[0]
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(here.stat().st_mode) == 0o700
    doc = json.loads(f.read_text())
    assert set(doc) == {"session_id", "ppid", "started_at"}
    assert doc["session_id"] == SID and doc["ppid"] == os.getpid()
    assert type(doc["started_at"]) is float
    assert "transcript" not in f.read_text() and "secret" not in f.read_text()
    assert [p.name for p in here.parent.iterdir()] == ["codex-here"]  # nothing else written


def test_hook_explicit_state_dir_argument(tmp_path):
    custom = tmp_path / "custom"
    r, _ = run_hook(tmp_path, json.dumps({"session_id": SID}).encode(), arg=str(custom))
    assert r.returncode == 0
    assert [p.name for p in (custom / "codex-here").iterdir()] == [f"{os.getpid()}.json"]


@pytest.mark.parametrize(
    "stdin",
    [
        b"",
        b"not json",
        b"[]",
        b'{"session_id": 5}',
        b"{}",
        b'{"session_id": "bad id with spaces"}',
        b'{"session_id": "../../etc/passwd"}',
        b'{"session_id": "a\\u0000b"}',
        b'{"session_id": "' + b"a" * 129 + b'"}',
        b'{"session_id": "x\\n"}',
        b'{"session_id": "' + SID.encode() + b'", "pad": "' + b"z" * 70000 + b'"}',
        b"\xff\xfe\x00",
    ],
    ids=["empty", "nonjson", "list", "int-sid", "missing", "spaces", "traversal", "nul",
         "too-long", "newline", "oversized", "binary"],
)
def test_hook_bad_input_exits_zero_and_writes_nothing(tmp_path, stdin):
    r, here = run_hook(tmp_path, stdin)
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")
    assert not here.exists() or list(here.iterdir()) == []


def test_hook_unwritable_state_dir_still_exits_zero(tmp_path):
    blocker = tmp_path / "state"
    blocker.write_text("a file where the directory should go")
    r, _ = run_hook(tmp_path, json.dumps({"session_id": SID}).encode())
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")


def test_hook_passes_bash_syntax_check():
    assert subprocess.run(["bash", "-n", str(HOOK)], check=False).returncode == 0
    sc = shutil.which("shellcheck")
    if sc:
        assert subprocess.run([sc, str(HOOK)], check=False).returncode == 0


# ---- the reader ------------------------------------------------------------------------------


def make_dir(tmp_path):
    d = tmp_path / "codex-here"
    d.mkdir(mode=0o700)
    d.chmod(0o700)
    return d


def put(d: Path, pid: int, *, sid=SID, ppid=None, started=1000.0, mode=0o600, doc=None):
    p = d / f"{pid}.json"
    body = doc if doc is not None else {
        "session_id": sid, "ppid": pid if ppid is None else ppid, "started_at": started}
    p.write_text(json.dumps(body))
    p.chmod(mode)
    return p


def test_reader_accepts_valid_file(tmp_path):
    d = make_dir(tmp_path)
    put(d, 4242)
    got = read_here_dir(d)
    assert [(r.pid, r.session_id, r.started_at) for r in got] == [(4242, SID, 1000.0)]


def test_reader_missing_directory_is_empty(tmp_path):
    assert read_here_dir(tmp_path / "nope") == []
    assert read_here_dir(None) == []


def test_reader_refuses_symlink_file(tmp_path):
    d = make_dir(tmp_path)
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"session_id": SID, "ppid": 7, "started_at": 1.0}))
    real.chmod(0o600)
    os.symlink(real, d / "7.json")
    assert read_here_dir(d) == []


def test_reader_refuses_symlinked_directory(tmp_path):
    d = make_dir(tmp_path)
    put(d, 5)
    link = tmp_path / "link"
    os.symlink(d, link)
    assert read_here_dir(link) == []


def test_reader_refuses_directory_entry(tmp_path):
    d = make_dir(tmp_path)
    (d / "9.json").mkdir()
    assert read_here_dir(d) == []


def test_reader_refuses_fifo_without_hanging(tmp_path):
    d = make_dir(tmp_path)
    os.mkfifo(d / "11.json", 0o600)
    assert read_here_dir(d) == []


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o604, 0o606, 0o777])
def test_reader_refuses_group_or_other_access(tmp_path, mode):
    d = make_dir(tmp_path)
    put(d, 21, mode=mode)
    assert read_here_dir(d) == []


def test_reader_refuses_open_directory_mode(tmp_path):
    d = make_dir(tmp_path)
    put(d, 22)
    d.chmod(0o755)
    assert read_here_dir(d) == []


def test_reader_refuses_foreign_owner(tmp_path, monkeypatch):
    d = make_dir(tmp_path)
    put(d, 23)
    monkeypatch.setattr(codex_mod, "_euid", lambda: UID + 1)
    assert read_here_dir(d) == []


def test_reader_refuses_oversize_file(tmp_path):
    d = make_dir(tmp_path)
    p = put(d, 24)
    p.write_text(json.dumps({"session_id": SID, "ppid": 24, "started_at": 1.0, "x": "a" * 5000}))
    p.chmod(0o600)
    assert read_here_dir(d) == []


def test_reader_read_is_bounded_not_only_stat_checked(tmp_path, monkeypatch):
    """The read itself is capped (a file can grow after fstat)."""
    d = make_dir(tmp_path)
    put(d, 25)
    reads: list[int] = []
    real_read = os.read

    def spy(fd, n):
        reads.append(n)
        return real_read(fd, n)

    monkeypatch.setattr(codex_mod.os, "read", spy)
    read_here_dir(d)
    assert reads and max(reads) <= codex_mod.MAX_HERE_BYTES + 1


def test_reader_refuses_file_that_grows_past_cap_after_fstat(tmp_path, monkeypatch):
    d = make_dir(tmp_path)
    put(d, 26)
    real_fstat = os.fstat
    big = {"n": 0}

    def liar(fd):
        st = real_fstat(fd)
        if stat.S_ISREG(st.st_mode):
            big["n"] += 1
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid,
                                   st.st_gid, 10, int(st.st_atime), int(st.st_mtime),
                                   int(st.st_ctime)))
        return st

    (d / "26.json").write_text(json.dumps(
        {"session_id": SID, "ppid": 26, "started_at": 1.0, "pad": "a" * 8000}))
    (d / "26.json").chmod(0o600)
    monkeypatch.setattr(codex_mod.os, "fstat", liar)
    assert read_here_dir(d) == []
    assert big["n"] >= 1


@pytest.mark.parametrize(
    "doc",
    [
        {"session_id": SID, "ppid": 31},
        {"session_id": SID, "ppid": 31, "started_at": 1.0, "extra": 1},
        {"session_id": 5, "ppid": 31, "started_at": 1.0},
        {"session_id": "bad id", "ppid": 31, "started_at": 1.0},
        {"session_id": "a" * 129, "ppid": 31, "started_at": 1.0},
        {"session_id": SID, "ppid": "31", "started_at": 1.0},
        {"session_id": SID, "ppid": True, "started_at": 1.0},
        {"session_id": SID, "ppid": 0, "started_at": 1.0},
        {"session_id": SID, "ppid": 31, "started_at": "1.0"},
        {"session_id": SID, "ppid": 31, "started_at": True},
        {"session_id": SID, "ppid": 32, "started_at": 1.0},  # does not match the file name
        [1, 2],
    ],
)
def test_reader_refuses_bad_schema(tmp_path, doc):
    d = make_dir(tmp_path)
    put(d, 31, doc=doc)
    assert read_here_dir(d) == []


@pytest.mark.parametrize(
    "text",
    ['{"session_id": "' + SID + '", "ppid": 31, "started_at": NaN}',
     '{"session_id": "' + SID + '", "ppid": 31, "started_at": Infinity}',
     '{"session_id": "a", "session_id": "' + SID + '", "ppid": 31, "started_at": 1.0}',
     "{not json", ""],
)
def test_reader_refuses_bad_json(tmp_path, text):
    d = make_dir(tmp_path)
    p = d / "31.json"
    p.write_text(text)
    p.chmod(0o600)
    assert read_here_dir(d) == []


def test_reader_ignores_odd_file_names(tmp_path):
    d = make_dir(tmp_path)
    put(d, 41)
    for name in ("x.json", "41.json.bak", "-3.json", ".41.json.tmp", "041.json"):
        p = d / name
        p.write_text(json.dumps({"session_id": SID, "ppid": 41, "started_at": 1.0}))
        p.chmod(0o600)
    assert [r.pid for r in read_here_dir(d)] == [41]


def test_reader_file_count_cap_256(tmp_path, monkeypatch):
    d = make_dir(tmp_path)
    for pid in range(1000, 1257):  # 257 files
        put(d, pid)
    opened: list[str] = []
    real_open = os.open

    def spy(path, flags, *a, **kw):
        opened.append(str(path))
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(codex_mod.os, "open", spy)
    got = read_here_dir(d)
    assert len(got) == 256
    assert len([p for p in opened if p.endswith(".json")]) == 256


def test_reader_never_opens_a_transcript_path(tmp_path, monkeypatch):
    d = make_dir(tmp_path)
    put(d, 51, doc={"session_id": SID, "ppid": 51, "started_at": 1.0,
                    "transcript_path": "/nonexistent/t.jsonl"})
    put(d, 52)
    opened: list[str] = []
    real_open = os.open

    def spy(path, flags, *a, **kw):
        opened.append(str(path))
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(codex_mod.os, "open", spy)
    got = read_here_dir(d)
    assert [r.pid for r in got] == [52]  # the extra-key file is refused outright
    assert not any("transcript" in p or p.startswith("/nonexistent") for p in opened)
    assert all(p.endswith(".json") or p == str(d) for p in opened)


# ---- the join to the process tree ------------------------------------------------------------


class FakeProc:
    def __init__(self, pid, ppid, argv, started=1000.0):
        self.pid, self._ppid, self._argv, self._started = pid, ppid, argv, started

    def ppid(self):
        return self._ppid

    def cmdline(self):
        return self._argv

    def cpu_percent(self, interval=None):
        return 2.5

    def memory_info(self):
        return type("M", (), {"rss": 8192})()

    def create_time(self):
        return self._started


def ctx():
    return Ctx(cfg=SourceCfg(), clock=lambda: 5000.0, log=logging.getLogger("t"))


def collect(procs, d):
    return CodexSource(lister=lambda: list(procs), here_dir=d).collect(ctx())


def test_session_row_has_address_name_and_task(tmp_path):
    d = make_dir(tmp_path)
    put(d, 300, started=1001.0)
    res = collect([FakeProc(300, 1, ["codex", "--model", "m"], started=1000.0)], d)
    assert len(res.sessions) == 1
    s = res.sessions[0]
    assert (s.pid, s.agent, s.session_id, s.address) == (300, "codex", SID, SID)
    assert s.name == "codex-" + SID[:8]
    assert s.task == "codex"
    assert s.cpu == 2.5 and s.rss == 8192 and s.started == 1000.0
    assert all(p.pid != 300 for p in res.procs)  # not shown twice


def test_pid_reused_by_later_process_is_ignored(tmp_path):
    d = make_dir(tmp_path)
    put(d, 301, started=1000.0)
    # a codex that started 100 s AFTER the hook wrote the file is not the process that wrote it
    res = collect([FakeProc(301, 1, ["codex"], started=1100.0)], d)
    assert res.sessions == []
    assert [p.pid for p in res.procs] == [301]  # still a plain process row


def test_pid_reused_by_non_codex_process_is_ignored(tmp_path):
    d = make_dir(tmp_path)
    put(d, 302, started=1000.0)
    res = collect([FakeProc(302, 1, ["/usr/bin/vim", "x"], started=999.0)], d)
    assert res.sessions == [] and res.procs == []


def test_small_clock_slack_is_tolerated(tmp_path):
    d = make_dir(tmp_path)
    put(d, 303, started=1000.0)
    res = collect([FakeProc(303, 1, ["codex"], started=1000.5)], d)
    assert len(res.sessions) == 1


def test_file_for_dead_pid_is_ignored_and_not_deleted(tmp_path):
    d = make_dir(tmp_path)
    p = put(d, 304)
    res = collect([FakeProc(1, 0, ["init"])], d)
    assert res.sessions == []
    assert p.exists()


def test_without_files_codex_still_reports_plain_rows(tmp_path):
    res = collect([FakeProc(305, 1, ["codex"])], make_dir(tmp_path))
    assert res.sessions == [] and [p.pid for p in res.procs] == [305]


# ---- you-are-here resolution -----------------------------------------------------------------


def codex_snapshot(tmp_path):
    d = make_dir(tmp_path)
    put(d, 600, started=1001.0)
    res = collect([FakeProc(600, 1, ["codex"], started=1000.0)], d)
    return Snapshot(version=SNAPSHOT_VERSION, taken=5000.0, host={}, sessions=res.sessions,
                    procs=res.procs, health=[])


def test_auto_resolves_codex_ancestor(tmp_path):
    snap = codex_snapshot(tmp_path)
    parents = {900: 800, 800: 600}
    assert commands.find_current_session(snap.sessions, 900, parents.get) == SID


def test_explicit_codex_id_marks_current(tmp_path):
    snap = codex_snapshot(tmp_path)
    views = build_views(snap, 5000.0, current_session_id=SID)
    assert views.banner is not None and views.banner.session is not None
    assert views.banner.session.agent == "codex"
    assert views.sessions[0].is_current
