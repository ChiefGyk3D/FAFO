"""Codex you-are-here: the SessionStart hook script and the bounded reader of its files."""

from __future__ import annotations

import json
import logging
import os
import re
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


def hook_python() -> str:
    text = HOOK.read_text(encoding="utf-8")
    return text.split("<<'PYEOF'\n", 1)[1].split("\nPYEOF\n", 1)[0]


def run_hook_in_process(base: Path, monkeypatch, *, euid: int | None = None) -> None:
    import io
    import runpy
    import sys

    stdin = type("In", (), {"buffer": io.BytesIO(json.dumps({"session_id": SID}).encode())})()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "argv", ["hook", str(base)])
    if euid is not None:
        monkeypatch.setattr(os, "geteuid", lambda: euid)
    script = base.parent / "hook_under_test.py"
    script.write_text(hook_python(), encoding="utf-8")
    runpy.run_path(str(script), run_name="hook")


def private(path: Path, mode: int = 0o700) -> Path:
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    path.chmod(mode)
    return path


def test_hook_refuses_symlinked_codex_here(tmp_path):
    base = private(tmp_path / "base")
    outside = private(tmp_path / "outside")
    (base / "codex-here").symlink_to(outside)
    r, _ = run_hook(tmp_path, json.dumps({"session_id": SID}).encode(), arg=str(base))
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")
    assert list(outside.iterdir()) == []


def test_hook_refuses_symlinked_base(tmp_path):
    outside = private(tmp_path / "outside")
    private(outside / "codex-here")
    (tmp_path / "base").symlink_to(outside)
    r, _ = run_hook(tmp_path, json.dumps({"session_id": SID}).encode(), arg=str(tmp_path / "base"))
    assert r.returncode == 0
    assert list((outside / "codex-here").iterdir()) == []


def test_hook_refuses_symlinked_component_below_base(tmp_path):
    outside = private(tmp_path / "outside")
    base = private(tmp_path / "base")
    (base / "codex-here").symlink_to(outside)
    base2 = private(tmp_path / "base2")
    (base2 / "x").symlink_to(base)
    r, _ = run_hook(tmp_path, json.dumps({"session_id": SID}).encode(), arg=str(base2 / "x"))
    assert r.returncode == 0
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o705, 0o777])
def test_hook_refuses_existing_open_directory(tmp_path, mode):
    base = private(tmp_path / "base")
    here = private(base / "codex-here", mode)
    r, _ = run_hook(tmp_path, json.dumps({"session_id": SID}).encode(), arg=str(base))
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")
    assert list(here.iterdir()) == []
    assert stat.S_IMODE(here.stat().st_mode) == mode  # left as found, never repaired in place


def test_hook_refuses_open_base_directory(tmp_path):
    base = private(tmp_path / "base", 0o755)
    r, _ = run_hook(tmp_path, json.dumps({"session_id": SID}).encode(), arg=str(base))
    assert r.returncode == 0
    assert list(base.iterdir()) == []


def test_hook_accepts_existing_private_directory(tmp_path):
    base = private(tmp_path / "base")
    here = private(base / "codex-here")
    r, _ = run_hook(tmp_path, json.dumps({"session_id": SID}).encode(), arg=str(base))
    assert r.returncode == 0
    assert [p.name for p in here.iterdir()] == [f"{os.getpid()}.json"]
    assert stat.S_IMODE((here / f"{os.getpid()}.json").stat().st_mode) == 0o600


def test_hook_in_process_writes_when_owner_matches(tmp_path, monkeypatch):
    base = private(tmp_path / "base")
    run_hook_in_process(base, monkeypatch)
    assert [p.name for p in (base / "codex-here").iterdir()] == [f"{os.getppid()}.json"]


def test_hook_refuses_directory_owned_by_someone_else(tmp_path, monkeypatch):
    base = private(tmp_path / "base")
    private(base / "codex-here")
    run_hook_in_process(base, monkeypatch, euid=UID + 1)
    assert list((base / "codex-here").iterdir()) == []


def test_hook_creates_missing_directories_private(tmp_path):
    base = tmp_path / "a" / "b" / "base"
    r, _ = run_hook(tmp_path, json.dumps({"session_id": SID}).encode(), arg=str(base))
    assert r.returncode == 0
    for d in (tmp_path / "a", tmp_path / "a" / "b", base, base / "codex-here"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d


def test_hook_leaves_no_temp_file_behind(tmp_path):
    base = private(tmp_path / "base")
    run_hook(tmp_path, json.dumps({"session_id": SID}).encode(), arg=str(base))
    assert not [p for p in (base / "codex-here").iterdir() if p.name.endswith(".tmp")]


TOKENS = [
    "ghp_" + "a" * 36,
    "sk-" + "A1b2" * 8,
    "AKIA" + "A" * 16,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc".replace(".", "-"),
    "plain-id_1",
    SID.upper().replace("-", "_"),
    SID[:-1],  # one digit short
    "g" + SID[1:],  # not hex
]


@pytest.mark.parametrize("sid", TOKENS)
def test_hook_refuses_non_uuid_session_id(tmp_path, sid):
    r, here = run_hook(tmp_path, json.dumps({"session_id": sid}).encode())
    assert r.returncode == 0
    assert not here.exists() or list(here.iterdir()) == []


@pytest.mark.parametrize("sid", [SID, SID.upper()])
def test_hook_accepts_uuid_either_case(tmp_path, sid):
    r, here = run_hook(tmp_path, json.dumps({"session_id": sid}).encode())
    assert r.returncode == 0
    assert json.loads((here / f"{os.getpid()}.json").read_text())["session_id"] == sid


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


def rd(d, pids=None):
    """Read the directory for the pids named in it (the production caller passes live pids)."""
    if pids is None:
        try:
            names = os.listdir(d) if d is not None else []
        except OSError:
            names = []
        pids = sorted(int(n[:-5]) for n in names if re.fullmatch(r"[1-9][0-9]*\.json", n))[:256]
    return read_here_dir(d, pids)


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
    got = rd(d)
    assert [(r.pid, r.session_id, r.started_at) for r in got] == [(4242, SID, 1000.0)]


def test_reader_missing_directory_is_empty(tmp_path):
    assert rd(tmp_path / "nope") == []
    assert rd(None) == []


def test_reader_refuses_symlink_file(tmp_path):
    d = make_dir(tmp_path)
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"session_id": SID, "ppid": 7, "started_at": 1.0}))
    real.chmod(0o600)
    os.symlink(real, d / "7.json")
    assert rd(d) == []


def test_reader_refuses_symlinked_directory(tmp_path):
    d = make_dir(tmp_path)
    put(d, 5)
    link = tmp_path / "link"
    os.symlink(d, link)
    assert rd(link) == []


def test_reader_refuses_directory_entry(tmp_path):
    d = make_dir(tmp_path)
    (d / "9.json").mkdir()
    assert rd(d) == []


def test_reader_refuses_fifo_without_hanging(tmp_path):
    d = make_dir(tmp_path)
    os.mkfifo(d / "11.json", 0o600)
    assert rd(d) == []


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o604, 0o606, 0o777])
def test_reader_refuses_group_or_other_access(tmp_path, mode):
    d = make_dir(tmp_path)
    put(d, 21, mode=mode)
    assert rd(d) == []


def test_reader_refuses_open_directory_mode(tmp_path):
    d = make_dir(tmp_path)
    put(d, 22)
    d.chmod(0o755)
    assert rd(d) == []


def test_reader_refuses_foreign_owner(tmp_path, monkeypatch):
    d = make_dir(tmp_path)
    put(d, 23)
    monkeypatch.setattr(codex_mod, "_euid", lambda: UID + 1)
    assert rd(d) == []


def test_reader_refuses_file_owned_by_someone_else_in_own_directory(tmp_path, monkeypatch):
    d = make_dir(tmp_path)
    put(d, 27)
    real_fstat = os.fstat

    def other_owner(fd):
        st = real_fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return st
        return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid + 1,
                               st.st_gid, st.st_size, int(st.st_atime), int(st.st_mtime),
                               int(st.st_ctime)))

    monkeypatch.setattr(codex_mod.os, "fstat", other_owner)
    assert rd(d) == []


def test_reader_refuses_oversize_file(tmp_path):
    d = make_dir(tmp_path)
    p = put(d, 24)
    p.write_text(json.dumps({"session_id": SID, "ppid": 24, "started_at": 1.0, "x": "a" * 5000}))
    p.chmod(0o600)
    assert rd(d) == []


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
    rd(d)
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
    assert rd(d) == []
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
    assert rd(d) == []


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
    assert rd(d) == []


def test_reader_reads_only_requested_pids(tmp_path):
    d = make_dir(tmp_path)
    put(d, 41)
    put(d, 42)
    assert [r.pid for r in read_here_dir(d, [41, 99])] == [41]


@pytest.mark.parametrize("bad", [0, -1, True, "41", 41.0, None, 2**40])
def test_reader_ignores_invalid_pid_values(tmp_path, bad):
    d = make_dir(tmp_path)
    put(d, 41)
    assert read_here_dir(d, [bad]) == []


def test_stale_files_cannot_hide_the_live_record(tmp_path):
    d = make_dir(tmp_path)
    for pid in range(1, 1001):  # 1000 stale records with low pids
        put(d, pid)
    put(d, 50000)
    got = read_here_dir(d, [50000])
    assert [r.pid for r in got] == [50000]


def test_reader_never_lists_the_directory(tmp_path, monkeypatch):
    d = make_dir(tmp_path)
    put(d, 61)
    for name in ("scandir", "listdir", "walk"):
        def boom(*a, _n=name, **k):
            raise AssertionError("directory listing: " + _n)
        monkeypatch.setattr(codex_mod.os, name, boom)
    assert [r.pid for r in read_here_dir(d, [61])] == [61]


def test_many_directory_entries_do_not_add_opens(tmp_path, monkeypatch):
    d = make_dir(tmp_path)
    for i in range(2000):
        (d / f"junk{i}").write_text("x")
    put(d, 71)
    opened: list[str] = []
    real_open = os.open

    def spy(path, flags, *a, **kw):
        opened.append(str(path))
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(codex_mod.os, "open", spy)
    assert [r.pid for r in read_here_dir(d, [71])] == [71]
    assert len(opened) == 2  # the directory and the one record


def test_live_pid_without_a_file_is_fine(tmp_path):
    d = make_dir(tmp_path)
    assert read_here_dir(d, [12345]) == []


def test_reader_file_count_cap_256(tmp_path, monkeypatch):
    d = make_dir(tmp_path)
    for pid in range(1000, 1257):  # 257 files
        put(d, pid)
    pids = list(range(1000, 1257))
    opened: list[str] = []
    real_open = os.open

    def spy(path, flags, *a, **kw):
        opened.append(str(path))
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(codex_mod.os, "open", spy)
    got = read_here_dir(d, pids)
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
    got = rd(d)
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
    res = collect([FakeProc(303, 1, ["codex"], started=1000.2)], d)
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


# ---- the start-time window --------------------------------------------------------------------

REC_AT = 1000.0


@pytest.mark.parametrize(
    "proc_started, accepted",
    [
        (999.9, True),
        (1000.0, True),
        (1000.0 + codex_mod.CLOCK_SLACK_S, True),  # upper limit
        (1000.0 + codex_mod.CLOCK_SLACK_S + 0.01, False),
        (1001.0, False),  # an unrelated later codex process
        (1000.0 - codex_mod.MAX_HOOK_DELAY_S, True),  # lower limit
        (1000.0 - codex_mod.MAX_HOOK_DELAY_S - 0.01, False),
        (1000.0 - 11 * 60, False),
    ],
)
def test_start_time_window(tmp_path, proc_started, accepted):
    d = make_dir(tmp_path)
    put(d, 700, started=REC_AT)
    res = collect([FakeProc(700, 1, ["codex"], started=proc_started)], d)
    assert (len(res.sessions) == 1) is accepted


def test_window_constants_are_tight_and_documented():
    assert codex_mod.CLOCK_SLACK_S <= 0.25
    assert codex_mod.MAX_HOOK_DELAY_S == 600


def test_real_process_create_time_is_not_after_wall_clock():
    """create_time (boot time + ticks) must not run ahead of time.time() by more than the slack."""
    import time

    import psutil

    proc = subprocess.Popen(["sleep", "5"])
    try:
        now = time.time()
        created = psutil.Process(proc.pid).create_time()
    finally:
        proc.kill()
        proc.wait()
    assert created <= now + codex_mod.CLOCK_SLACK_S
    assert now - created < 5


# ---- session id shape and the supplied clock ---------------------------------------------------


@pytest.mark.parametrize("sid", TOKENS)
def test_reader_refuses_non_uuid_session_id(tmp_path, sid):
    d = make_dir(tmp_path)
    put(d, 810, sid=sid)
    assert rd(d) == []


@pytest.mark.parametrize("sid", [SID, SID.upper()])
def test_reader_accepts_uuid_either_case(tmp_path, sid):
    d = make_dir(tmp_path)
    put(d, 811, sid=sid)
    assert [r.session_id for r in rd(d)] == [sid]


def test_token_shaped_id_never_reaches_a_session(tmp_path):
    d = make_dir(tmp_path)
    put(d, 812, sid="ghp_" + "a" * 36, started=1000.0)
    res = collect([FakeProc(812, 1, ["codex"], started=1000.0)], d)
    assert res.sessions == [] and [p.pid for p in res.procs] == [812]


NOW = 5000.0  # the clock in ctx()


@pytest.mark.parametrize(
    "rec_at, accepted",
    [
        (NOW, True),
        (NOW + 0.25, True),
        (NOW + 0.26, False),
        (NOW + 300.0, False),
        (NOW + 1e9, False),
    ],
)
def test_record_from_the_future_is_not_attributed(tmp_path, rec_at, accepted):
    d = make_dir(tmp_path)
    put(d, 820, started=rec_at)
    res = collect([FakeProc(820, 1, ["codex"], started=rec_at - 1.0)], d)
    assert (len(res.sessions) == 1) is accepted
    if not accepted:
        assert [p.pid for p in res.procs] == [820]
