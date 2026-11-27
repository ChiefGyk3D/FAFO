"""Table, JSON and sessions output: goldens at two widths, width limits, no control codes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
from fafo.render.table import format_json, format_sessions, format_table
from fafo.render.view import build_views

NOW = 1_000_000.0
FIXTURES = Path(__file__).parent / "fixtures"
SID_A = "11111111-aaaa-4aaa-8aaa-000000000001"
SID_B = "22222222-bbbb-4bbb-8bbb-000000000002"
SID_C = "33333333-cccc-4ccc-8ccc-000000000003"


def sess(pid, sid, name, project, state="busy", task="work", agent="claude", as_of=NOW, branch="main"):
    return Session(
        pid=pid, agent=agent, session_id=sid, project=Stamped(project, as_of),
        project_rule="children-git", task=task, state=state, branch=branch,
        worktree="/work/example", started=NOW - 4320, cpu=3.0, rss=512 * 1024 * 1024,
        name=name, address=sid,
    )


def proc(pid, session_pid, agent="claude", cmd="git status"):
    return Proc(
        pid=pid, ppid=1, session_pid=session_pid, agent=agent, cmd=cmd,
        cpu=1.0, rss=10 * 1024 * 1024, started=NOW - 60,
    )


def golden_snapshot() -> Snapshot:
    return Snapshot(
        version=SNAPSHOT_VERSION,
        taken=NOW - 2,
        host={"load1": 4.1, "cpu_percent": 20.0, "mem_percent": 61.0, "gpus": [{"util": 78}]},
        sessions=[
            sess(100, SID_A, "src-1", "reach-alpha", task="PR 482 review fix", branch="wt-482"),
            sess(200, SID_B, "src-2", "beta-service", state="waiting", task="input needed"),
            sess(300, SID_C, "src-3", "gamma-tool", state="idle", task="", as_of=NOW - 40),
            sess(0, "run-9", "agent-run", "alpha", agent="copilot", task="fix lint"),
        ],
        procs=[
            proc(101, 100, cmd="pytest -q"),
            proc(102, 100, cmd="git log"),
            proc(201, 200, cmd="ruff check"),
            proc(500, None, agent="codex", cmd="codex exec"),
            proc(501, None, agent="gemini", cmd="gemini"),
        ],
        llm=LlmServer(
            models=[LoadedModel("model-a:12b", 8 * 1024**3, 128000, NOW + 240)],
            queue=0,
            inflight=[Inflight("model-a:12b", 100, NOW - 5)],
            gpu={},
        ),
        runners=[
            Runner("r1", "busy", "owner/repo", "ci", "build", "compile", 3, 7, 130.0, 3.2, 61.0, -1.0),
            Runner("r2", "idle", "", "", "", "", 0, 0, 0.0, -1.0, -1.0, -1.0),
        ],
        quota={"claude": {"5h": 41, "wk": 63}},
        health=[
            SourceHealth("claude", "ok", NOW - 2, ""),
            SourceHealth("runners", "stale", NOW - 6, "timeout"),
        ],
    )


def views(snap=None, now=NOW, **kw):
    return build_views(golden_snapshot() if snap is None else snap, now, **kw)


@pytest.mark.parametrize("width", [80, 144])
def test_golden_table(width):
    out = format_table(views(current_session_id=SID_B), width)
    expected = (FIXTURES / f"table_{width}.txt").read_text(encoding="utf-8")
    assert out == expected


@pytest.mark.parametrize("width", [20, 40, 60, 80, 100, 144, 200])
def test_no_line_exceeds_width(width):
    for v in (views(current_session_id=SID_A), views(now=NOW + 30), views(now=NOW + 300)):
        out = format_table(v, width)
        assert out.strip()
        assert all(len(line) <= width for line in out.splitlines())


def test_no_control_or_ansi_codes():
    out = format_table(views(current_session_id=SID_A), 100)
    assert not any(ord(c) < 32 and c != "\n" for c in out)
    assert not any(0x7F <= ord(c) < 0xA0 for c in out)


def test_hostile_input_never_reaches_output():
    esc = chr(27)
    evil = f"{esc}]0;pwn{chr(7)}t{esc}[31mask" + "y" * 5000
    snap = golden_snapshot()
    snap.sessions[0].task = evil
    snap.procs[0].cmd = evil
    out = format_table(views(snap), 100)
    assert esc not in out and chr(7) not in out
    assert all(len(line) <= 100 for line in out.splitlines())


def test_narrow_width_drops_low_priority_columns_keeps_project_and_state():
    out = format_table(views(), 60)
    head = next(line for line in out.splitlines() if "PROJECT" in line)
    assert "STATE" in head
    assert "RSS" not in head and "BRANCH" not in head


def test_current_session_marked_with_star_and_banner():
    out = format_table(views(current_session_id=SID_B), 100)
    lines = out.splitlines()
    assert lines[2].startswith("you are here: src-2")
    marked = [line for line in lines if line.startswith("* ")]
    assert len(marked) == 1 and "beta-service" in marked[0]


def test_unknown_session_banner_in_table():
    out = format_table(views(current_session_id="nope"), 100)
    assert "\nunknown session\n" in out


def test_children_indented_under_owner_and_groups_listed():
    out = format_table(views(), 144)
    lines = out.splitlines()
    i = next(n for n, line in enumerate(lines) if "reach-alpha" in line)
    assert "pytest -q" in lines[i + 1] and "git log" in lines[i + 2]
    assert "UNATTACHED" in out
    for name in ("codex (1)", "gemini (1)", "copilot cloud (1)"):
        assert name in out


def test_no_collector_text_and_age():
    assert "collector not running" in format_table(build_views(None, NOW), 80)
    old = format_table(views(now=NOW + 125), 100)
    assert "collector not running (last update 2m ago)" in old
    assert "~2m" in old


def test_stale_rows_show_age():
    out = format_table(views(now=NOW + 30), 120)
    assert "~32s" in out
    assert "view 32s" in out


def test_unknown_runner_metrics_are_question_marks():
    out = format_table(views(), 144)
    line = next(ln for ln in out.splitlines() if ln.lstrip().startswith("r2"))
    assert line.split()[-3:] == ["?", "?", "?"]


def test_more_indicator_on_300_sessions():
    snap = golden_snapshot()
    snap.sessions = [sess(i + 1, f"id{i:04d}", f"n{i:04d}", "p") for i in range(300)]
    snap.procs = []
    out = format_table(views(snap), 100)
    assert "+100 more sessions" in out
    assert out.count("\n") < 260


def test_sessions_output_exact_lines_with_marker():
    out = format_sessions(views(current_session_id=SID_B))
    lines = out.splitlines()
    assert lines == [
        f"* src-2  {SID_B}  beta-service",
        f"  src-1  {SID_A}  reach-alpha",
        f"  src-3  {SID_C}  gamma-tool",
    ]


def test_sessions_output_excludes_cloud_rows_and_empty_is_empty():
    assert "run-9" not in format_sessions(views())
    assert format_sessions(build_views(None, NOW)) == ""


def test_json_roundtrip_has_name_and_address():
    doc = json.loads(format_json(golden_snapshot(), NOW))
    assert doc["status"] == "ok"
    assert doc["sessions"][0]["address"] == SID_A
    assert doc["sessions"][0]["name"] == "src-1"
    assert list(doc) == sorted(doc)


def test_json_stable_and_none():
    assert format_json(golden_snapshot(), NOW) == format_json(golden_snapshot(), NOW)
    assert json.loads(format_json(None, NOW)) == {"status": "no-collector"}
    assert json.loads(format_json(golden_snapshot(), NOW + 30))["status"] == "stale"


def _strict(text):
    def refuse(name):
        raise AssertionError(f"non-standard JSON constant {name}")

    return json.loads(text, parse_constant=refuse)


def test_format_json_is_strict_json_with_nonfinite_values():
    snap = golden_snapshot()
    snap.host["load1"] = float("nan")
    snap.host["nested"] = {"a": [float("inf"), -float("inf"), 1.5]}
    snap.quota["claude"]["5h"] = float("nan")
    out = format_json(snap, NOW)
    doc = _strict(out)
    assert doc["host"]["load1"] is None
    assert doc["host"]["nested"] == {"a": [None, None, 1.5]}
    assert doc["quota"]["claude"]["5h"] is None
    assert "NaN" not in out and "Infinity" not in out


def test_format_json_nonfinite_age_and_deep_nesting_do_not_raise():
    snap = golden_snapshot()
    deep: object = 1.0
    for _ in range(500):
        deep = [deep]
    snap.host["deep"] = deep
    snap.taken = float("nan")
    _strict(format_json(snap, NOW))


def test_table_shows_last_known_project_with_tilde():
    snap = golden_snapshot()
    snap.sessions[0].project = Stamped("chatty-kathy", NOW)
    snap.sessions[0].project_source = "last"
    out = format_table(views(snap), 144)
    assert "chatty-kathy~" in out
