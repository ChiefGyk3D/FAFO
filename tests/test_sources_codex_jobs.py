"""Codex plugin job files: classification, privacy of titles, hostile files, caps, store, table."""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fafo.config import SourceCfg
from fafo.model import SNAPSHOT_VERSION, JobRow, JobSummary, Snapshot
from fafo.render.table import format_json, format_table
from fafo.render.view import build_views
from fafo.source import Ctx
from fafo.sources import codex_jobs
from fafo.sources.codex_jobs import CodexJobsSource
from fafo.store import read_snapshot, write_snapshot

NOW = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC).timestamp()
ESC = chr(27)
BEL = chr(7)
CTX = Ctx(cfg=SourceCfg(), clock=lambda: NOW, log=logging.getLogger("test.codex_jobs"))
# Token-shaped and made up: a long mixed run that the credential masker must hide.
FAKE_SECRET = "ghp_" + "A1b2C3d4" * 5
# A 100-character token-shaped value built from pieces. It has no known credential prefix, so
# only the long-run rule can mask it, and a 35-character prefix of it is too short to match.
CANARY = "A1b2C3d4" * 12 + "Zq9w"


def iso(offset_s: float) -> str:
    return datetime.fromtimestamp(NOW + offset_s, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def put(d: Path, name: str, doc, mode: int = 0o600) -> Path:
    p = d / name
    if isinstance(doc, bytes):
        p.write_bytes(doc)
    else:
        p.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    p.chmod(mode)
    return p


def job(status="running", pid=None, title="Example job", started=-120, completed=None, **extra):
    d = {
        "status": status,
        "phase": "working",
        "pid": pid or os.getpid(),
        "kindLabel": "rescue",
        "title": title,
        "startedAt": iso(started),
    }
    if completed is not None:
        d["completedAt"] = iso(completed)
    d.update(extra)
    return d


@pytest.fixture
def jd(tmp_path):
    d = tmp_path / "jobs"
    d.mkdir(mode=0o700)
    return d


def collect(d, **kw):
    return CodexJobsSource(str(d), **kw).collect(CTX)


def test_running_done_recent_done_old_are_classified(jd):
    put(jd, "a.json", job("running"))
    put(jd, "b.json", job("completed", title="Finished", started=-900, completed=-300))
    put(jd, "c.json", job("completed", title="Old", started=-9000, completed=-7200))
    put(jd, "d.json", job("failed", title="Broke", started=-900, completed=-60))
    put(jd, "e.json", job("running", pid=4_000_000))  # not alive
    s = collect(jd).jobs
    assert (s.running, s.done, s.failed) == (1, 1, 1)
    assert [r.state for r in s.rows] == ["running", "done", "failed"]
    assert s.rows[0].kind == "rescue" and s.rows[0].phase == "working"


def test_window_minutes_option(jd):
    put(jd, "b.json", job("completed", completed=-600))
    assert collect(jd, window_minutes=5).jobs.done == 0
    assert collect(jd, window_minutes=15).jobs.done == 1


def test_plugin_layout_one_level_down(jd):
    sub = jd / "abc123" / "jobs"
    sub.mkdir(parents=True)
    put(sub, "j.json", job("completed", completed=-10))
    assert collect(jd).jobs.done == 1


def test_titles_hidden_by_default(jd):
    put(jd, "a.json", job("running", title="SECRET-PROMPT-TEXT"))
    s = collect(jd).jobs
    assert s.running == 1 and s.rows[0].title == ""
    assert "SECRET-PROMPT-TEXT" not in repr(s)


def test_titles_shown_masked_and_capped_with_option(jd):
    title = f"{ESC}[31mfix\tthe\nbug --token {FAKE_SECRET} " + "x" * 500
    put(jd, "a.json", job("running", title=title))
    t = collect(jd, show_titles=True).jobs.rows[0].title
    assert t.startswith("fix") and "<redacted>" in t and len(t) <= 60
    assert FAKE_SECRET not in t and ESC not in t and "\n" not in t and "\t" not in t


def test_caplog_never_has_titles(jd, caplog):
    put(jd, "a.json", job("running", title="PROMPT-IN-TITLE"))
    put(jd, "bad.json", "{not json PROMPT-IN-TITLE")
    with caplog.at_level(logging.DEBUG):
        collect(jd, show_titles=True)
    assert "PROMPT-IN-TITLE" not in caplog.text
    assert "1 unreadable" in caplog.text  # something was logged, counts only


def _hostile(jd):
    put(jd, "ok.json", job("running"))
    target = jd.parent / "target.json"
    target.write_text(json.dumps(job("running", title="LINKED")))
    (jd / "link.json").symlink_to(target)
    os.mkfifo(jd / "fifo.json")
    (jd / "dir.json").mkdir()
    put(jd, "gw.json", job("running", title="GW"), mode=0o660)
    put(jd, "ow.json", job("running", title="OW"), mode=0o602)
    put(jd, "big.json", json.dumps(job("running", title="B" * 70_000)))
    put(jd, "nan.json", '{"status":"running","pid":NaN}')
    put(jd, "nan2.json", '{"status":"running","startedAt":"2026-01-01T00:00:00Z","x":Infinity}')
    put(jd, "deep.json", "[" * 30000 + "]" * 30000)
    deep = f'{{"status":"running","pid":{os.getpid()},"x":{"[" * 20}1{"]" * 20}}}'
    put(jd, "deep2.json", deep)
    put(jd, "enum.json", job("exploding"))
    put(jd, "dup.json", f'{{"status":"running","status":"completed","pid":{os.getpid()}}}')
    put(jd, "types.json", {"status": "running", "pid": "1", "title": 5})
    put(jd, "boolpid.json", {"status": "running", "pid": True})
    put(jd, "ts.json", job("completed", completed=-10, completedAt="9999-01-01T00:00:00Z"))
    put(jd, "future.json", job("completed", completed=+86400))
    put(jd, "top.json", "[1,2]")
    put(jd, "bin.json", b"\xff\xfe\x00{")
    put(jd, "empty.json", "")


def test_hostile_files_never_raise_or_leak(jd):
    _hostile(jd)
    s = collect(jd, show_titles=True).jobs
    assert (s.running, s.done, s.failed) == (1, 0, 0)  # only ok.json counts
    text = repr(s)
    for leaked in ("LINKED", "GW", "OW", "BBBB"):
        assert leaked not in text
    # 18 invalid: link.json, fifo.json, dir.json (non-regular), gw, ow (mode), big (size),
    # nan, nan2, deep, deep2, enum, dup, types, boolpid, ts, top, bin, empty. ok and future
    # are not invalid (future is a valid file outside the window).
    assert s.invalid == 18


def test_huge_title_with_controls_and_secret_is_bounded(jd):
    title = f"{ESC}]0;pwn{BEL} {FAKE_SECRET} " + "z" * 30_000
    put(jd, "a.json", job("running", title=title))
    t = collect(jd, show_titles=True).jobs.rows[0].title
    assert len(t) <= 60 and FAKE_SECRET not in t and BEL not in t and ESC not in t


def test_secret_behind_controls_is_masked_before_any_cut(jd):
    """The 400-character slice used to run before masking: 365 control characters left only
    the canary's first 35 characters, which the masker did not see as a credential."""
    assert len(CANARY) == 100
    put(jd, "a.json", job("running", title=ESC * 365 + CANARY))
    t = collect(jd, show_titles=True).jobs.rows[0].title
    assert "<redacted>" in t
    assert not any(CANARY[i : i + 12] in t for i in range(len(CANARY) - 11))


# Same canary as the redactor tests: 100 characters, no known prefix, built from pieces.
TITLE_CANARY = "A1b2C3d4" * 12 + "Zq9w"


@pytest.mark.parametrize("controls", [3965, 4096])
def test_title_controls_then_canary_leaks_no_piece(jd, controls):
    put(jd, "a.json", job("running", title=ESC * controls + TITLE_CANARY))
    t = collect(jd, show_titles=True).jobs.rows[0].title
    assert "<redacted>" in t
    assert not any(TITLE_CANARY[i : i + 8] in t for i in range(len(TITLE_CANARY) - 7))


def test_title_70000_controls_is_refused_whole_and_leaks_nothing(jd):
    """70000 escaped controls make the file exceed the 64 KB cap: it is refused, never shown."""
    put(jd, "a.json", job("running", title=ESC * 70000 + TITLE_CANARY))
    s = collect(jd, show_titles=True).jobs
    assert (s.running, s.invalid, s.rows) == (0, 1, [])
    assert TITLE_CANARY[:8] not in repr(s)


def test_hostile_labels_are_dropped(jd):
    put(jd, "a.json", job("running", phase=f"w{ESC}[2Jx", kindLabel="r" + BEL))
    r = collect(jd).jobs.rows[0]
    assert r.phase == "" and r.kind == ""


def test_missing_dir_is_absent(tmp_path):
    assert collect(tmp_path / "nope").status == "absent"


def test_root_symlink_refused(tmp_path, jd):
    link = tmp_path / "linkdir"
    link.symlink_to(jd)
    assert collect(link).status == "absent"


def test_rows_capped_and_extra_counted(jd):
    for i in range(80):
        put(jd, f"j{i:03}.json", job("completed", completed=-10 - i))
    s = collect(jd).jobs
    assert s.done == 80 and len(s.rows) == codex_jobs.MAX_ROWS and s.hidden == 16


def test_entry_cap_counts_unexamined(jd, monkeypatch):
    monkeypatch.setattr(codex_jobs, "MAX_ENTRIES", 10)
    monkeypatch.setattr(codex_jobs, "MAX_READ", 10)
    for i in range(25):
        put(jd, f"j{i:03}.json", job("completed", completed=-10))
    s = collect(jd).jobs
    assert s.done == 10 and s.unread == 15


def test_read_cap_counts_unexamined(jd, monkeypatch):
    monkeypatch.setattr(codex_jobs, "MAX_READ", 3)
    for i in range(8):
        put(jd, f"j{i}.json", job("completed", completed=-10))
    s = collect(jd).jobs
    assert s.done == 3 and s.unread == 5


def test_descriptors_stay_bounded_across_many_id_dirs(jd, monkeypatch):
    """400 made-up <id>/jobs folders. Peak open descriptors must stay at root + one subdirectory
    + one jobs folder, not two per id, and the results must not change."""
    for i in range(400):
        sub = jd / f"id{i:03}" / "jobs"
        sub.mkdir(parents=True)
        if i % 10 == 0:
            put(sub, "j.json", job("completed", completed=-10))
    live: set[int] = set()
    peak = [0]
    real_open, real_close = os.open, os.close

    def counting_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        live.add(fd)
        peak[0] = max(peak[0], len(live))
        return fd

    def counting_close(fd):
        live.discard(fd)
        real_close(fd)

    monkeypatch.setattr(codex_jobs.os, "open", counting_open)
    monkeypatch.setattr(codex_jobs.os, "close", counting_close)
    s = collect(jd).jobs
    assert (s.done, s.invalid, s.unread) == (40, 0, 0)
    assert 2 <= peak[0] <= 3


def test_entry_scan_stops_after_cap_even_when_stat_fails(jd, monkeypatch):
    """Entries whose stat() fails used to be free, so a 100000-entry listing was read whole.
    Every entry pulled counts against the cap before stat is tried."""
    pulled = [0]

    class Entry:
        def __init__(self, i: int) -> None:
            self.name = f"e{i}.json"

        def stat(self, follow_symlinks: bool = True):
            raise OSError("vanished")

    class Listing:
        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

        def __iter__(self):
            for i in range(100_000):
                pulled[0] += 1
                yield Entry(i)

    monkeypatch.setattr(codex_jobs.os, "scandir", lambda _fd: Listing())
    s = collect(jd).jobs
    assert pulled[0] == codex_jobs.MAX_ENTRIES + codex_jobs.MAX_COUNT_EXTRA
    assert (s.done, s.unread) == (0, codex_jobs.MAX_COUNT_EXTRA)


def test_non_regular_json_entries_count_invalid_without_open(jd, monkeypatch):
    """A symlink, a FIFO and a directory named *.json are counted invalid; none is opened."""
    put(jd, "ok.json", job("running"))
    target = jd.parent / "target.json"
    target.write_text(json.dumps(job("running", title="LINKED")))
    (jd / "link.json").symlink_to(target)
    os.mkfifo(jd / "fifo.json")
    (jd / "dir.json").mkdir()
    opened: list[str] = []
    real_open = os.open

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(codex_jobs.os, "open", spy)
    s = collect(jd, show_titles=True).jobs
    assert (s.running, s.invalid) == (1, 3)
    assert not {"link.json", "fifo.json", "dir.json"} & set(opened)
    assert "LINKED" not in repr(s)


def test_non_json_names_ignored(jd):
    put(jd, "a.txt", job("running"))
    s = collect(jd).jobs
    assert s.running == 0 and s.invalid == 0


# -- store, table ---------------------------------------------------------------------------


def snap_with(jobs):
    return Snapshot(version=SNAPSHOT_VERSION, taken=NOW, host={}, jobs=jobs)


def sample_jobs(title="Example job"):
    return JobSummary(
        running=1, done=2, failed=0, rows=[JobRow("running", 30.0, "rescue", "working", title)]
    )


def test_store_round_trip_and_key_omitted_when_empty(tmp_path):
    p = tmp_path / "s" / "snap.json"
    write_snapshot(p, snap_with(None))
    assert "jobs" not in json.loads(p.read_text())
    assert read_snapshot(p).jobs is None
    write_snapshot(p, snap_with(sample_jobs()))
    assert read_snapshot(p).jobs == sample_jobs()


def test_old_snapshot_without_jobs_loads(tmp_path):
    p = tmp_path / "s" / "snap.json"
    write_snapshot(p, snap_with(None))
    assert "jobs" not in json.loads(p.read_text())
    assert read_snapshot(p) is not None


GOOD_ROW = {"state": "done", "age": 1.0, "kind": "", "phase": "", "title": ""}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda j: j.update(rows=[{**GOOD_ROW, "state": "bogus"}]),
        lambda j: j.update(rows="x"),
        lambda j: j.update(extra=1),
        lambda j: j.update(rows=[{"state": "done"}]),
        lambda j: j.update(rows=[GOOD_ROW] * 65),
    ],
)
def test_store_refuses_bad_jobs_section(tmp_path, mutate):
    p = tmp_path / "s" / "snap.json"
    write_snapshot(p, snap_with(sample_jobs()))
    assert read_snapshot(p) is not None  # the unmutated file loads: the refusal below is real
    doc = json.loads(p.read_text())
    mutate(doc["jobs"])
    p.write_text(json.dumps(doc))
    p.chmod(0o600)
    assert read_snapshot(p) is None


def test_store_repairs_bad_count(tmp_path):
    p = tmp_path / "s" / "snap.json"
    write_snapshot(p, snap_with(sample_jobs()))
    doc = json.loads(p.read_text())
    doc["jobs"]["running"] = -3
    p.write_text(json.dumps(doc))
    p.chmod(0o600)
    assert read_snapshot(p).jobs.running == 0


def test_table_shows_jobs_section_without_titles():
    out = format_table(build_views(snap_with(sample_jobs(title="")), NOW))
    assert "JOBS" in out
    assert "1 running, 2 done recently, 0 failed recently" in out
    assert "rescue working" in out


def test_table_shows_title_when_present():
    out = format_table(build_views(snap_with(sample_jobs(title="Example job")), NOW))
    assert "Example job" in out


def test_table_has_no_jobs_section_without_source():
    assert "JOBS" not in format_table(build_views(snap_with(None), NOW))
    assert '"jobs"' not in format_json(snap_with(None), NOW)
    assert '"jobs"' in format_json(snap_with(sample_jobs()), NOW)


def test_guard_bites_group_writable_bit_is_real(jd):
    p = put(jd, "x.json", job(), mode=0o660)
    assert stat.S_IMODE(p.stat().st_mode) & stat.S_IWGRP


# -- config and registry --------------------------------------------------------------------


def _cfg(tmp_path, body):
    from fafo.config import load_config

    f = tmp_path / "c.toml"
    f.write_text(body, encoding="utf-8")
    return load_config(f)


def test_config_options_and_defaults_off(tmp_path):
    from fafo.config import load_config

    assert load_config(None).sources["codex_jobs"].enabled is False
    cfg = _cfg(
        tmp_path,
        '[sources.codex_jobs]\njobs_dir = "~/j"\nwindow_minutes = 30\nshow_titles = true\n',
    )
    assert cfg.sources["codex_jobs"].options == {
        "jobs_dir": "~/j", "window_minutes": 30, "show_titles": True,
    }


@pytest.mark.parametrize(
    "line", ["window_minutes = 0", "window_minutes = 1441", 'window_minutes = "5"',
             "window_minutes = true", 'show_titles = "yes"', "show_titles = 1"],
)
def test_config_refuses_bad_values(tmp_path, line):
    from fafo.config import ConfigError

    with pytest.raises(ConfigError):
        _cfg(tmp_path, f'[sources.codex_jobs]\njobs_dir = "/x"\n{line}\n')


def test_registry_needs_jobs_dir_and_builds_with_defaults(tmp_path):
    from fafo.config import ConfigError
    from fafo.registry import build

    with pytest.raises(ConfigError):
        build(_cfg(tmp_path, "[sources.codex_jobs]\nenabled = true\n"), {})
    res = build(_cfg(tmp_path, '[sources.codex_jobs]\njobs_dir = "/x"\n'), {})
    src = next(s for s in res.sources if s.name == "codex_jobs")
    assert src.show_titles is False and src.window_s == 3600.0


def test_file_swapped_for_symlink_after_listing_is_refused(jd, monkeypatch):
    """The listing said regular file; before the open it became a link to a valid job file.
    The no-follow open must refuse it (a path check would have passed)."""
    target = put(jd.parent, "target.json", job("running", title="LINKED"))  # a valid job file
    victim = put(jd, "v.json", job("running", title="ORIGINAL"))
    real = codex_jobs._candidates

    def swap(root):
        out = real(root)
        victim.unlink()
        victim.symlink_to(target)
        return out

    monkeypatch.setattr(codex_jobs, "_candidates", swap)
    s = collect(jd, show_titles=True).jobs
    assert s.running == 0 and s.invalid == 1 and "LINKED" not in repr(s)
