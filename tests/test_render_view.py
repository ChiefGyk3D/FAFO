"""View model: grouping, you-are-here, staleness, caps, sanitizing. No terminal, no network."""

from __future__ import annotations

from pathlib import Path

from fafo.model import (
    SNAPSHOT_VERSION,
    Inflight,
    LlmServer,
    LoadedModel,
    Proc,
    Runner,
    Session,
    Snapshot,
    SourceHealth,
    Stamped,
)
from fafo.render.view import build_views, read_views
from fafo.store import write_snapshot

NOW = 1_000_000.0
SID_A = "11111111-aaaa-4aaa-8aaa-000000000001"
SID_B = "22222222-bbbb-4bbb-8bbb-000000000002"
SID_C = "33333333-cccc-4ccc-8ccc-000000000003"


def sess(pid, sid, name, project, state="busy", task="work", agent="claude", as_of=NOW, **kw):
    return Session(
        pid=pid,
        agent=agent,
        session_id=sid,
        project=Stamped(project, as_of),
        project_rule="children-git",
        task=task,
        state=state,
        branch=kw.get("branch", "main"),
        worktree=kw.get("worktree", "/work/example"),
        started=NOW - 4320,
        cpu=3.0,
        rss=512 * 1024 * 1024,
        name=name,
        address=sid,
    )


def proc(pid, session_pid, agent="claude", cmd="git status"):
    return Proc(
        pid=pid, ppid=1, session_pid=session_pid, agent=agent, cmd=cmd,
        cpu=1.0, rss=10 * 1024 * 1024, started=NOW - 60,
    )


def snap(sessions=(), procs=(), taken=NOW, **kw):
    return Snapshot(
        version=SNAPSHOT_VERSION,
        taken=taken,
        host=kw.get("host", {"load1": 4.1, "cpu_percent": 20.0, "mem_percent": 61.0}),
        sessions=list(sessions),
        procs=list(procs),
        llm=kw.get("llm"),
        runners=kw.get("runners", []),
        quota=kw.get("quota", {}),
        health=kw.get("health", []),
    )


def three_same_dir():
    return snap(
        sessions=[
            sess(100, SID_A, "src-1", "alpha"),
            sess(200, SID_B, "src-2", "beta"),
            sess(300, SID_C, "src-3", "gamma"),
        ],
        procs=[
            proc(101, 100, cmd="pytest alpha"),
            proc(102, 100, cmd="git log"),
            proc(201, 200, cmd="ruff check"),
            proc(301, 300, cmd="make beta"),
        ],
    )


def test_three_sessions_same_directory_own_children():
    v = build_views(three_same_dir(), NOW)
    by_name = {n.name: n for n in v.sessions}
    assert sorted(c.pid for c in by_name["src-1"].children) == [101, 102]
    assert [c.pid for c in by_name["src-2"].children] == [201]
    assert [c.pid for c in by_name["src-3"].children] == [301]
    assert by_name["src-1"].project == "alpha"
    assert v.unattached == []


def test_unowned_codex_gemini_other_in_own_groups():
    s = snap(
        sessions=[sess(100, SID_A, "src-1", "alpha")],
        procs=[
            proc(500, None, agent="codex", cmd="codex exec"),
            proc(501, None, agent="gemini", cmd="gemini"),
            proc(502, None, agent="ollama", cmd="client"),
            proc(503, 4242, agent="codex", cmd="orphan of a dead session"),
        ],
    )
    v = build_views(s, NOW)
    groups = {g.name: [p.pid for p in g.procs] for g in v.unattached}
    assert groups == {"codex": [500, 503], "gemini": [501], "other": [502]}
    assert v.sessions[0].children == []


def test_copilot_cloud_rows_in_own_group_not_under_sessions():
    cloud = sess(0, "run-1", "agent-run", "alpha", agent="copilot", state="busy")
    v = build_views(snap(sessions=[sess(100, SID_A, "src-1", "alpha"), cloud]), NOW)
    assert [n.name for n in v.sessions] == ["src-1"]
    groups = {g.name: g for g in v.unattached}
    assert list(groups) == ["copilot cloud"]
    assert [n.name for n in groups["copilot cloud"].sessions] == ["agent-run"]


def test_current_session_first_marked_and_banner():
    v = build_views(three_same_dir(), NOW, current_session_id=SID_C)
    assert v.sessions[0].session_id == SID_C
    assert v.sessions[0].is_current is True
    assert [n.is_current for n in v.sessions] == [True, False, False]
    assert v.banner is not None and v.banner.session is v.sessions[0]
    for part in ("src-3", "gamma", "work", "pid 300", "busy"):
        assert part in v.banner.text


def test_unknown_session_id_exact_banner_and_no_current():
    v = build_views(three_same_dir(), NOW, current_session_id="does-not-exist")
    assert v.banner is not None
    assert v.banner.text == "unknown session"
    assert v.banner.session is None
    assert not any(n.is_current for n in v.sessions)


def test_no_current_id_means_no_banner():
    assert build_views(three_same_dir(), NOW).banner is None


def test_sort_current_then_busy_waiting_idle_then_project_name():
    s = snap(
        sessions=[
            sess(1, "i1", "n1", "zeta", state="idle"),
            sess(2, "w1", "n2", "alpha", state="waiting"),
            sess(3, "b2", "n3", "beta", state="busy"),
            sess(4, "b1", "n4", "alpha", state="busy"),
            sess(5, "cur", "n5", "zzz", state="idle"),
        ]
    )
    v = build_views(s, NOW, current_session_id="cur")
    assert [n.session_id for n in v.sessions] == ["cur", "b1", "b2", "w1", "i1"]


def test_snapshot_none_is_no_collector():
    v = build_views(None, NOW)
    assert v.status == "no-collector"
    assert v.age_s is None
    assert v.message == "collector not running"
    assert v.sessions == []


def test_none_snapshot_with_session_id_still_unknown_session():
    assert build_views(None, NOW, current_session_id=SID_A).banner.text == "unknown session"


def test_old_snapshot_no_collector_with_age_and_stale_rows():
    v = build_views(three_same_dir(), NOW + 125)
    assert v.status == "no-collector"
    assert v.age_s == 125
    assert v.message == "collector not running (last update 2m ago)"
    assert v.sessions and all(n.stale for n in v.sessions)
    assert all(c.stale for n in v.sessions for c in n.children)


def test_between_thresholds_is_stale_with_every_row_marked():
    v = build_views(three_same_dir(), NOW + 30)
    assert v.status == "stale"
    assert "30s" in v.message
    assert all(n.stale and n.age == "30s" for n in v.sessions)


def test_fresh_snapshot_is_ok_and_not_stale():
    v = build_views(three_same_dir(), NOW + 2)
    assert v.status == "ok"
    assert not any(n.stale for n in v.sessions)


def test_per_field_staleness_from_as_of():
    s = snap(
        sessions=[
            sess(1, "old", "n1", "alpha", as_of=NOW - 40),
            sess(2, "new", "n2", "beta", as_of=NOW - 1),
        ]
    )
    by = {n.session_id: n for n in build_views(s, NOW).sessions}
    assert by["old"].stale is True and by["old"].age == "40s"
    assert by["new"].stale is False


def test_health_strip_ages():
    s = snap(
        health=[
            SourceHealth("claude", "ok", NOW - 2, ""),
            SourceHealth("runners", "stale", NOW - 6, "timeout"),
        ]
    )
    v = build_views(s, NOW)
    assert [(h.name, h.status, h.age) for h in v.health] == [
        ("claude", "ok", "2s"),
        ("runners", "stale", "6s"),
    ]


def test_unknown_runner_metrics_show_question_mark():
    r = Runner("r1", "busy", "o/r", "ci", "build", "compile", 3, 7, 130.0, -1.0, -1.0, 40.0)
    v = build_views(snap(runners=[r]), NOW)
    rv = v.runners[0]
    assert (rv.load, rv.mem, rv.disk) == ("?", "?", "40%")
    assert rv.step == "3/7"
    assert rv.elapsed == "2m10s"


def test_llm_view_expiry_queue_inflight():
    llm = LlmServer(
        models=[LoadedModel("m1:12b", 8 * 1024**3, 128000, NOW + 240)],
        queue=2,
        inflight=[Inflight("m1:12b", 100, NOW - 5), Inflight("m1:12b", None, NOW - 5)],
        gpu={},
    )
    v = build_views(snap(sessions=[sess(100, SID_A, "src-1", "alpha")], llm=llm), NOW)
    assert v.llm is not None
    m = v.llm.models[0]
    assert (m.name, m.vram, m.context, m.expires) == ("m1:12b", "8.0G", "128k", "4m")
    assert v.llm.queue == 2
    assert [i.session for i in v.llm.inflight] == ["src-1", "?"]


def test_quota_flattened():
    v = build_views(snap(quota={"claude": {"5h": 41, "wk": 63}, "codex": "ok"}), NOW)
    assert {q.name: q.text for q in v.quota} == {"claude": "5h 41  wk 63", "codex": "ok"}


def test_session_cap_reports_hidden_count():
    sessions = [sess(i + 1, f"id{i:04d}", f"n{i:04d}", "p") for i in range(300)]
    v = build_views(snap(sessions=sessions), NOW)
    assert len(v.sessions) == 200
    assert v.sessions_hidden == 100


def test_children_cap_reports_hidden_count():
    procs = [proc(1000 + i, 100) for i in range(300)]
    v = build_views(snap(sessions=[sess(100, SID_A, "src-1", "alpha")], procs=procs), NOW)
    node = v.sessions[0]
    assert len(node.children) == 256
    assert node.children_hidden == 44


def test_control_chars_and_huge_task_sanitized_and_capped():
    esc = chr(27)
    evil = f"{esc}]0;pwn{chr(7)}ok{chr(0)}{esc}[31mred" + "x" * 5000
    s = snap(
        sessions=[
            sess(1, SID_A, f"n{esc}[2Jame", "proj" + chr(8), task=evil, branch=evil, worktree=evil)
        ],
        procs=[proc(2, 1, cmd=evil)],
    )
    n = build_views(s, NOW).sessions[0]
    assert len(n.task) <= 120
    assert n.task.startswith("okred")
    for text in (n.name, n.project, n.task, n.branch, n.worktree, n.children[0].cmd):
        assert not any(ord(c) < 32 or 0x7F <= ord(c) < 0xA0 for c in text)
    assert n.name == "name" and n.project == "proj"
    assert len(n.children[0].cmd) <= 200


def test_read_views_reads_file_and_missing_file(tmp_path: Path):
    p = tmp_path / "snap.json"
    assert read_views(p, NOW).status == "no-collector"
    write_snapshot(p, three_same_dir())
    v = read_views(p, NOW, current_session_id=SID_B)
    assert v.status == "ok" and v.sessions[0].session_id == SID_B


def test_build_views_is_pure_and_deterministic():
    s = three_same_dir()
    assert build_views(s, NOW) == build_views(s, NOW)


def test_last_known_project_gets_trailing_tilde_and_source():
    last = sess(100, SID_A, "a", "chatty-kathy")
    last.project_source = "last"
    folder = sess(200, SID_B, "b", "src")
    folder.project_source = "folder"
    live = sess(300, SID_C, "c", "alpha")
    v = build_views(snap(sessions=[last, folder, live]), NOW)
    by = {n.name: n for n in v.sessions}
    assert by["a"].project == "chatty-kathy~" and by["a"].project_source == "last"
    assert by["b"].project == "src" and by["b"].project_source == "folder"
    assert by["c"].project == "alpha" and by["c"].project_source == "live"
