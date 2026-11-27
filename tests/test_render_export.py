"""views.json export: schema, no per-session state, size bound, atomic 0600 write."""

from __future__ import annotations

import json
import os
import stat
import threading

from fafo.model import SNAPSHOT_VERSION, Proc, Session, Snapshot, Stamped
from fafo.render import export
from fafo.render.export import views_to_dict, write_views
from fafo.render.view import build_views

NOW = 1_000_000.0
CTRL = chr(27) + "[31m" + chr(7) + chr(0)


def sess(pid, sid, **kw):
    return Session(
        pid=pid, agent="claude", session_id=sid, project=Stamped(kw.get("project", "proj"), NOW),
        project_rule="cwd-name", task=kw.get("task", "work"), state="busy", branch="main",
        worktree="/work/example", started=NOW - 100, cpu=1.0, rss=1024,
        name=kw.get("name", f"n{pid}"), address=sid,
    )


def proc(pid, owner):
    return Proc(pid, owner, owner, "claude", f"cmd{pid}", 0.5, 2048, NOW - 10)


def snap(n_sessions=2, n_children=0, task="work"):
    sessions = [sess(i + 1, f"sid-{i + 1}", task=task) for i in range(n_sessions)]
    procs = [proc(100000 + s * 1000 + c, s + 1) for s in range(n_sessions) for c in range(n_children)]
    return Snapshot(version=SNAPSHOT_VERSION, taken=NOW, host={}, sessions=sessions, procs=procs)


def test_round_trip_has_schema_and_every_session_id(tmp_path):
    views = build_views(snap(3), NOW, current_session_id="sid-2")
    path = tmp_path / "views.json"
    write_views(path, views, generated=NOW)
    doc = json.loads(path.read_text())
    assert doc["schema"] == 1
    assert doc["generated"] == NOW
    assert doc["status"] == "ok"
    assert [s["session_id"] for s in doc["sessions"]] == ["sid-2", "sid-1", "sid-3"] or sorted(
        s["session_id"] for s in doc["sessions"]
    ) == ["sid-1", "sid-2", "sid-3"]
    for key in ("health", "unattached", "llm", "runners", "quota", "host"):
        assert key in doc
    assert {"name", "address", "project", "task", "state", "children", "stale"} <= set(
        doc["sessions"][0]
    )


def test_is_current_always_false_and_banner_none():
    views = build_views(snap(3), NOW, current_session_id="sid-2")
    assert views.sessions[0].is_current and views.banner is not None  # the input does carry it
    doc = views_to_dict(views, generated=NOW)
    assert doc["banner"] is None
    assert all(s["is_current"] is False for s in doc["sessions"])


def test_no_control_characters_in_encoded_text(tmp_path):
    views = build_views(snap(2, task="a" + CTRL + "b"), NOW)
    path = tmp_path / "views.json"
    write_views(path, views, generated=NOW)
    text = path.read_text()
    assert text  # something was written
    assert not [c for c in text if ord(c) < 32 or ord(c) == 127]
    assert "task" in text


def test_no_collector_views_still_valid(tmp_path):
    doc = views_to_dict(build_views(None, NOW), generated=NOW)
    assert doc["status"] == "no-collector"
    assert doc["sessions"] == [] and doc["banner"] is None and doc["age_s"] is None
    path = tmp_path / "views.json"
    write_views(path, build_views(None, NOW), generated=NOW)
    assert json.loads(path.read_text())["status"] == "no-collector"


def test_oversize_is_trimmed_to_valid_json_under_bound(tmp_path):
    views = build_views(snap(300, 256), NOW)
    path = tmp_path / "views.json"
    # the untrimmed encoding really is over the bound, so the trim path is what is tested
    assert len(export._encode(views_to_dict(views, generated=NOW))) > export.MAX_BYTES
    write_views(path, views, generated=NOW)
    raw = path.read_bytes()
    assert len(raw) <= export.MAX_BYTES < 4 * 1024 * 1024
    doc = json.loads(raw)
    assert doc["sessions"]
    assert all(len(s["children"]) <= 20 for s in doc["sessions"])
    assert any(s["children_hidden"] > 0 for s in doc["sessions"])
    assert doc["sessions_hidden"] == 100


def test_trim_falls_back_to_zero_children(tmp_path, monkeypatch):
    views = build_views(snap(5, 30), NOW)
    monkeypatch.setattr(export, "MAX_BYTES", 6000)
    doc = json.loads(export.encode_bounded(views_to_dict(views, generated=NOW)))
    assert all(s["children"] == [] and s["children_hidden"] == 30 for s in doc["sessions"])


def test_mode_is_0600_and_no_temp_left(tmp_path):
    path = tmp_path / "views.json"
    write_views(path, build_views(snap(1), NOW), generated=NOW)
    assert stat.S_IMODE(os.stat(path).st_mode) == stat.S_IRUSR | stat.S_IWUSR
    assert [p.name for p in tmp_path.iterdir()] == ["views.json"]


def test_failed_write_leaves_old_file_and_no_temp(tmp_path, monkeypatch):
    path = tmp_path / "views.json"
    write_views(path, build_views(snap(1), NOW), generated=NOW)
    before = path.read_bytes()

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(export.os, "replace", boom)
    try:
        write_views(path, build_views(snap(2), NOW), generated=NOW)
        raise AssertionError("expected OSError")
    except OSError:
        pass
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["views.json"]


def test_reader_never_sees_partial_json_during_writes(tmp_path):
    path = tmp_path / "views.json"
    big = build_views(snap(40, 20), NOW)
    write_views(path, big, generated=NOW)
    stop = threading.Event()
    bad: list[str] = []
    reads = [0]

    def reader():
        while not stop.is_set():
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                bad.append("missing")
                continue
            reads[0] += 1
            try:
                json.loads(data)
            except ValueError:
                bad.append(f"partial {len(data)}")

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        for i in range(200):
            write_views(path, big, generated=NOW + i)
    finally:
        stop.set()
        t.join(5)
    assert reads[0] > 0
    assert not bad, bad[:3]
