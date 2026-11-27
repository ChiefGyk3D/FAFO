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
    assert doc["schema"] == 2
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


def big_snapshot(n=300):
    sessions = [
        sess(i + 1, f"sid-{i + 1:04d}", name=f"name{i + 1}", project=f"p{i % 7}") for i in range(n)
    ]
    return Snapshot(version=SNAPSHOT_VERSION, taken=NOW, host={}, sessions=sessions)


def test_hidden_session_found_in_index(tmp_path):
    s = big_snapshot()
    views = build_views(s, NOW)
    assert len(views.sessions) == 200
    path = tmp_path / "views.json"
    write_views(path, views, snapshot=s, generated=NOW)
    doc = json.loads(path.read_text())
    shown = {x["session_id"] for x in doc["sessions"]}
    hidden = [x for x in doc["session_index"] if x["session_id"] not in shown]
    assert len(hidden) == 100 and doc["sessions_hidden"] == 100
    pid = hidden[0]["pid"]  # a session the display cap dropped
    assert hidden[0]["name"] == f"name{pid}" and hidden[0]["address"] == f"sid-{pid:04d}"
    assert len(doc["session_index"]) == 300


def test_index_order_cap_and_cloud_excluded():
    s = big_snapshot(5)
    s.sessions.reverse()
    s.sessions.append(
        Session(
            pid=0, agent="copilot", session_id="cloud1", project=Stamped("c", NOW),
            project_rule="r", task="t", state="busy", branch="b", worktree="w",
            started=1.0, cpu=0.0, rss=0,
        )
    )
    doc = views_to_dict(build_views(s, NOW), snapshot=s, generated=NOW)
    assert [x["pid"] for x in doc["session_index"]] == [1, 2, 3, 4, 5]
    assert set(doc["session_index"][0]) == {
        "session_id", "name", "pid", "project", "state", "address",
    }
    many = big_snapshot(2500)
    idx = views_to_dict(build_views(many, NOW), snapshot=many, generated=NOW)["session_index"]
    assert len(idx) == 2000 and idx[-1]["pid"] == 2000


def test_index_is_sanitized():
    s = big_snapshot(1)
    s.sessions[0].name = "a" + CTRL + "b"
    s.sessions[0].address = "x" * 500
    doc = views_to_dict(build_views(s, NOW), snapshot=s, generated=NOW)
    text = json.dumps(doc["session_index"], ensure_ascii=False)
    entry = doc["session_index"][0]
    assert chr(27) not in text and chr(7) not in text and chr(0) not in text
    assert len(entry["address"]) <= 128 and not [c for c in entry["name"] if ord(c) < 32]
    assert entry["name"]


def test_trimmed_case_keeps_index_intact(tmp_path):
    sessions = [sess(i + 1, f"sid-{i + 1:04d}") for i in range(300)]
    procs = [proc(100000 + s * 1000 + c, s + 1) for s in range(300) for c in range(256)]
    s = Snapshot(version=SNAPSHOT_VERSION, taken=NOW, host={}, sessions=sessions, procs=procs)
    path = tmp_path / "views.json"
    write_views(path, build_views(s, NOW), snapshot=s, generated=NOW)
    raw = path.read_bytes()
    assert len(raw) <= export.MAX_BYTES
    doc = json.loads(raw)
    assert [x["pid"] for x in doc["session_index"]] == list(range(1, 301))


def test_session_trim_keeps_index_when_sessions_alone_suffice(monkeypatch):
    s = big_snapshot(50)
    monkeypatch.setattr(export, "MAX_BYTES", 12000)
    doc = json.loads(export.encode_bounded(views_to_dict(build_views(s, NOW), snapshot=s)))
    assert len(doc["session_index"]) == 50 and doc["session_index_truncated"] is False
    assert doc["session_index_count"] == 50 and doc["index_hidden"] == 0
    assert len(doc["sessions"]) < 50 and doc["sessions_hidden"] == 50 - len(doc["sessions"])


def test_index_truncated_from_the_end_with_counts(monkeypatch):
    s = big_snapshot(120)
    monkeypatch.setattr(export, "MAX_BYTES", 3000)
    raw = export.encode_bounded(views_to_dict(build_views(s, NOW), snapshot=s, generated=NOW))
    assert len(raw) <= 3000
    doc = json.loads(raw)
    n = len(doc["session_index"])
    assert 0 < n < 120
    assert [x["pid"] for x in doc["session_index"]] == list(range(1, n + 1))
    assert doc["session_index_count"] == 120 and doc["index_hidden"] == 120 - n
    assert doc["session_index_truncated"] is True


def test_index_counts_when_over_the_entry_cap():
    s = big_snapshot(2100)
    doc = views_to_dict(build_views(s, NOW), snapshot=s, generated=NOW)
    assert len(doc["session_index"]) == 2000 and doc["session_index_count"] == 2100
    assert doc["index_hidden"] == 100 and doc["session_index_truncated"] is True


def test_index_field_caps():
    s = big_snapshot(1)
    x = s.sessions[0]
    x.session_id, x.name, x.address = "i" * 500, "n" * 500, "a" * 500
    x.project, x.state = Stamped("p" * 500, NOW), "s" * 500
    e = views_to_dict(build_views(s, NOW), snapshot=s)["session_index"][0]
    assert (len(e["name"]), len(e["project"]), len(e["state"])) == (48, 48, 16)
    assert len(e["session_id"]) == 128 and len(e["address"]) == 128


def test_worst_case_input_always_fits_the_bound(tmp_path):
    wide = chr(0x4E2D)  # 6 bytes escaped
    astral = chr(0x1F600)  # 12 bytes escaped
    text = (wide + astral) * 100
    sessions = [
        Session(
            pid=i + 1, agent="claude", session_id=text, project=Stamped(text, NOW),
            project_rule=text, task=text, state=text, branch=text, worktree=text,
            started=NOW - 5, cpu=1.0, rss=1, name=text, address=text,
        )
        for i in range(2000)
    ]
    procs = [
        Proc(100000 + s * 1000 + c, s + 1, s + 1, text, text, 0.5, 2048, NOW - 10)
        for s in range(200)
        for c in range(256)
    ] + [Proc(900000 + i, 1, None, "codex", text, 0.5, 1, NOW) for i in range(600)]
    snap_ = Snapshot(version=SNAPSHOT_VERSION, taken=NOW, host={}, sessions=sessions, procs=procs)
    views = build_views(snap_, NOW)
    path = tmp_path / "views.json"
    write_views(path, views, snapshot=snap_, generated=NOW)
    raw = path.read_bytes()
    assert len(raw) <= export.MAX_BYTES < 4 * 1024 * 1024
    doc = json.loads(raw)
    assert doc["session_index_count"] == 2000
    assert doc["session_index_truncated"] is True and doc["index_hidden"] > 0
    assert doc["index_hidden"] == 2000 - len(doc["session_index"])
    assert 0 < len(doc["session_index"]) < 2000  # trimmed, not dropped by the minimal fallback


def test_minimal_fallback_when_nothing_else_fits(monkeypatch):
    s = big_snapshot(3)
    monkeypatch.setattr(export, "MAX_BYTES", 400)
    raw = export.encode_bounded(views_to_dict(build_views(s, NOW), snapshot=s, generated=NOW))
    doc = json.loads(raw)
    assert doc["schema"] == 2 and doc["session_index"] == [] and doc["session_index_count"] == 3
    assert doc["session_index_truncated"] is True


def test_write_creates_missing_parents_owner_only(tmp_path):
    path = tmp_path / "a" / "b" / "views.json"
    write_views(path, build_views(snap(1), NOW), generated=NOW)
    assert json.loads(path.read_text())["schema"] == 2
    for d in (tmp_path / "a", tmp_path / "a" / "b"):
        assert stat.S_IMODE(os.stat(d).st_mode) == stat.S_IRWXU
    assert stat.S_IMODE(os.stat(path).st_mode) == stat.S_IRUSR | stat.S_IWUSR


def test_existing_directory_mode_untouched(tmp_path):
    d = tmp_path / "shared"
    d.mkdir()
    os.chmod(d, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)
    before = stat.S_IMODE(os.stat(d).st_mode)
    write_views(d / "views.json", build_views(snap(1), NOW), generated=NOW)
    assert stat.S_IMODE(os.stat(d).st_mode) == before


def test_views_json_carries_source_and_tilde_label():
    sn = snap(2)
    sn.sessions[0].project_source = "last"
    doc = views_to_dict(build_views(sn, NOW), snapshot=sn, generated=NOW)
    node = next(n for n in doc["sessions"] if n["session_id"] == "sid-1")
    assert node["project_source"] == "last" and node["project"].endswith("~")
    idx = next(e for e in doc["session_index"] if e["session_id"] == "sid-1")
    assert idx["project"].endswith("~")


def test_session_index_keeps_marker_for_long_names_both_paths():
    sn = snap(2)
    sn.sessions[0].project = Stamped("p" * 60, NOW)
    sn.sessions[0].project_source = "last"
    for snapshot in (sn, None):
        doc = views_to_dict(build_views(sn, NOW), snapshot=snapshot, generated=NOW)
        idx = next(e for e in doc["session_index"] if e["session_id"] == "sid-1")
        assert idx["project"].endswith("~") and len(idx["project"]) <= 48, snapshot is None
