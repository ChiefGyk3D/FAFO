"""Copilot cloud source tests. Fixtures only, no network."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from fafo.config import SourceCfg
from fafo.source import Ctx
from fafo.sources import copilot
from fafo.sources.copilot import CopilotSource, parse_agent_tasks

FIX = Path(__file__).parent / "fixtures"
NOW = 1_800_000_000.0
REPO = "example-org/example-repo"
REVIEW_PATH = "dynamic/agents/copilot-pull-request-reviewer"


def _load(name: str):
    return json.loads((FIX / name).read_text())


def _ctx() -> Ctx:
    return Ctx(cfg=SourceCfg(), clock=lambda: NOW, log=logging.getLogger("t"))


class FakeRuns:
    def __init__(self, runs: list[dict]):
        self.runs_data = runs
        self.calls: list[tuple[str, str]] = []

    def runs(self, repo: str, status: str) -> list[dict]:
        self.calls.append((repo, status))
        return [
            r for r in self.runs_data if not isinstance(r, dict) or r.get("status") == status
        ]


class FakeTasks:
    def __init__(self, tasks):
        self.data = tasks

    def tasks(self):
        return self.data


def _run(rid, status="in_progress", **kw):
    base = {
        "id": rid, "name": "Run", "event": "dynamic", "actor": {"login": "Copilot"},
        "head_branch": "b", "status": status, "path": REVIEW_PATH,
        "run_started_at": "2026-11-27T12:00:00Z",
    }
    base.update(kw)
    return base


def _collect(runs, repos=(REPO,)):
    return CopilotSource(FakeRuns(runs), list(repos)).collect(_ctx())


def test_name_and_capability():
    assert CopilotSource(None, []).name == "copilot"
    assert copilot.CAPABILITY.startswith("partial:")


def test_reviewer_review_and_configured_path_is_session():
    res = _collect(_load("copilot_runs.json"))
    by_id = {s.session_id: s for s in res.sessions}
    assert set(by_id) == {"run-1001"}
    assert by_id["run-1001"].task.startswith("review")
    sessions = CopilotSource(
        FakeRuns(_load("copilot_runs.json")), [REPO],
        session_paths=frozenset({"dynamic/copilot-swe-agent/copilot"}),
    ).collect(_ctx()).sessions
    by_id = {s.session_id: s for s in sessions}
    assert set(by_id) == {"run-1001", "run-1002"}
    assert by_id["run-1002"].task.startswith("session:")


def test_non_copilot_work_excluded():
    # pull_request by Copilot, dynamic by a person, dynamic outside dynamic/ path
    res = _collect(_load("copilot_runs.json"))
    ids = {s.session_id for s in res.sessions}
    assert not ids & {"run-1003", "run-1004", "run-1005"}


def test_state_mapping_and_fields():
    runs = [_run(1001, path=REVIEW_PATH, head_branch="feature/x"),
            _run(1002, status="queued", path=REVIEW_PATH)]
    res = _collect(runs)
    by_id = {s.session_id: s for s in res.sessions}
    assert by_id["run-1001"].state == "busy"
    assert by_id["run-1002"].state == "waiting"
    s = by_id["run-1001"]
    assert (s.pid, s.agent, s.cpu, s.rss, s.worktree) == (0, "copilot", 0.0, 0, "")
    assert s.branch == "feature/x"
    assert s.project_rule == "api"
    assert s.started == 1795780800.0


def test_project_is_bare_repo_name():
    res = _collect(_load("copilot_runs.json"))
    for s in res.sessions:
        assert s.project.value == "example-repo"
        assert s.project.as_of == NOW
    assert "example-org" not in json.dumps([s.task for s in res.sessions])


def test_both_statuses_requested():
    api = FakeRuns([])
    CopilotSource(api, [REPO]).collect(_ctx())
    assert sorted(api.calls) == [(REPO, "in_progress"), (REPO, "queued")]


def test_garbage_entries_skipped():
    garbage = [None, 5, "x", [], {}, {"id": "nan", "event": "dynamic"},
               _run(True), _run(2, actor="Copilot"), _run(3, actor=None),
               _run(4, path=None), _run(5, run_started_at="garbage"), _run(6)]
    res = _collect(garbage)
    ids = {s.session_id for s in res.sessions}
    assert ids == {"run-5", "run-6"}
    assert {s.session_id: s for s in res.sessions}["run-5"].started == 0.0


def test_non_list_runs_result_is_error():
    class Bad:
        def runs(self, repo, status):
            return {"workflow_runs": []}

    with pytest.raises(TypeError):
        CopilotSource(Bad(), [REPO]).collect(_ctx())


def test_run_cap_per_repo():
    runs = [_run(i) for i in range(1, 80)]
    res = _collect(runs)
    assert len(res.sessions) == copilot.MAX_RUNS_PER_REPO == 50


def test_control_chars_sanitized():
    esc = chr(27)
    bell = chr(7)
    runs = [_run(1, name=f"{esc}]0;pwn{bell}ev{esc}[31mil\nname", head_branch=f"br{esc}[2Jx")]
    s = _collect(runs).sessions[0]
    for text in (s.task, s.branch):
        assert esc not in text and bell not in text and "\n" not in text
    assert "pwn" not in s.task
    assert s.branch == "brx"


def test_repo_with_url_or_owner_gives_bare_name():
    s = _collect([_run(1)], repos=["https://example.com/example-org/example-repo"]).sessions[0]
    assert s.project.value == "example-repo"


def test_both_apis_absent_is_empty_ok():
    res = CopilotSource(None, [REPO]).collect(_ctx())
    assert res.sessions == [] and res.status is None


def test_runs_api_exception_propagates():
    class Boom:
        def runs(self, repo, status):
            raise RuntimeError("429")

    with pytest.raises(RuntimeError):
        CopilotSource(Boom(), [REPO]).collect(_ctx())


def test_agent_tasks_exception_propagates():
    class Boom:
        def tasks(self):
            raise RuntimeError("401")

    with pytest.raises(RuntimeError):
        CopilotSource(None, [REPO], agent_tasks=Boom()).collect(_ctx())


def test_agent_tasks_fixture_states_and_terminal_dropped():
    sessions = parse_agent_tasks(_load("agent_tasks.json"), now=NOW, repos=[REPO])
    by_id = {s.session_id: s for s in sessions}
    assert set(by_id) == {"task-t-1", "task-t-2", "task-t-3", "task-t-4"}
    assert by_id["task-t-1"].state == "busy"
    assert by_id["task-t-2"].state == "waiting"
    assert by_id["task-t-3"].state == "waiting"
    assert by_id["task-t-4"].state == "idle"
    assert by_id["task-t-1"].branch == "copilot/parser"
    assert by_id["task-t-1"].project.value == "example-repo"
    assert by_id["task-t-1"].pid == 0


def test_agent_tasks_cap_and_garbage():
    tasks = [{"id": str(i), "state": "queued"} for i in range(150)]
    for t in tasks:
        t["repo"] = REPO
    assert len(parse_agent_tasks(tasks, now=NOW, repos=[REPO])) == 100
    junk = [None, 3, {"id": 1, "state": "queued", "repo": REPO},
            {"id": "a", "state": "weird", "repo": REPO}, {"id": "b", "repo": REPO},
            {"id": "ok", "state": "idle", "artifacts": "nope", "repo": REPO}]
    out = parse_agent_tasks(junk, now=NOW, repos=[REPO])
    assert [s.session_id for s in out] == ["task-ok"]
    assert parse_agent_tasks("nonsense", now=NOW, repos=[REPO]) == []
    assert parse_agent_tasks({"tasks": "x"}, now=NOW, repos=[REPO]) == []


def test_agent_tasks_used_when_supplied_and_merged_with_runs():
    src = CopilotSource(FakeRuns([_run(9)]), [REPO],
                        agent_tasks=FakeTasks(_load("agent_tasks.json")))
    ids = {s.session_id for s in src.collect(_ctx()).sessions}
    assert ids == {"run-9", "task-t-1", "task-t-2", "task-t-3", "task-t-4"}
    only = CopilotSource(None, [REPO], agent_tasks=FakeTasks(_load("agent_tasks.json")))
    assert len(only.collect(_ctx()).sessions) == 4


def test_unknown_dynamic_path_counted_not_shown(caplog):
    runs = [_run(1, path="dynamic/copilot-swe-agent/copilot"), _run(2, path="dynamic/other"),
            _run(3, path="dynamic/" + "x" * 300), _run(4)]
    src = CopilotSource(FakeRuns(runs), [REPO])
    with caplog.at_level(logging.DEBUG, logger="t"):
        res = src.collect(_ctx())
    assert [s.session_id for s in res.sessions] == ["run-4"]
    assert src.unclassified == 3
    assert "x" * 121 not in caplog.text
    src.collect(_ctx())
    assert src.unclassified == 3  # reset per collect, not accumulated


def test_unclassified_count_capped():
    runs = [_run(i, path=f"dynamic/p{i}") for i in range(1, 60)]
    src = CopilotSource(FakeRuns(runs), [REPO])
    src.collect(_ctx())
    assert 0 < src.unclassified <= copilot.MAX_UNCLASSIFIED


def test_agent_tasks_filtered_to_configured_repos():
    tasks = [
        {"id": "a", "state": "queued", "repo": "example-org/example-repo"},
        {"id": "b", "state": "queued", "repo": "example-org/other-repo"},
        {"id": "c", "state": "queued"},
        {"id": "d", "state": "queued", "repo": 5},
    ]
    out = parse_agent_tasks(tasks, now=NOW, repos=[REPO])
    assert [s.session_id for s in out] == ["task-a"]
    assert out[0].project.value == "example-repo"
    assert "other-repo" not in json.dumps([s.project.value for s in out])
    assert parse_agent_tasks(tasks, now=NOW, repos=[]) == []
    src = CopilotSource(None, [], agent_tasks=FakeTasks(tasks))
    assert src.collect(_ctx()).sessions == []
