"""Copilot cloud source: Actions runs started by Copilot, plus Agent tasks when supplied.

Honest about coverage (see CAPABILITY). Identification of Actions runs is OBSERVED, not
documented: a Copilot code review run has event "dynamic", actor "Copilot" and a path
under "dynamic/". Any other such run is treated as a coding-agent session and labelled
"unverified". Ordinary workflows that Copilot triggers on its PRs are not Copilot work.

The Agent tasks API (public preview) needs a user-level token, so it is only used when
a client is supplied by the caller. Nothing here reads credentials or does I/O: both
APIs are injected protocols, and any exception from them propagates so the collector
keeps the previous rows and marks the source stale or down.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Protocol

from ..model import Session, Stamped
from ..redact import sanitize_text
from ..source import Ctx, SourceResult

CAPABILITY = (
    "partial: actions runs (observed identification) and agent tasks when a client is "
    "supplied; cloud-sandbox sessions need the Agent tasks API"
)

MAX_RUNS_PER_REPO = 50
MAX_TASKS = 100
_SCAN_CAP = 200  # entries examined per API call, before filtering
_REVIEW_PATH = "dynamic/agents/copilot-pull-request-reviewer"
_RUN_STATUS_STATE = {"in_progress": "busy", "queued": "waiting"}
_TASK_STATE = {
    "queued": "waiting",
    "waiting_for_user": "waiting",
    "in_progress": "busy",
    "idle": "idle",
}  # completed, failed, timed_out, cancelled are terminal and dropped
_NAME_LEN = 60
_TASK_LEN = 200
_ID_LEN = 128


class RunsApi(Protocol):
    def runs(self, repo: str, status: str) -> list[dict]: ...


class AgentTasksApi(Protocol):
    def tasks(self) -> list[dict]: ...


def _repo_name(repo: str) -> str:
    """Bare repository name only: never the owner or a URL."""
    if not isinstance(repo, str):
        return ""
    last = repo.strip().rstrip("/").rsplit("/", 1)[-1]
    last = last.removesuffix(".git")
    return sanitize_text(last, 100)


def _epoch(value: Any) -> float:
    if not isinstance(value, str) or len(value) > 64:
        return 0.0
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _str(value: Any, limit: int) -> str:
    return sanitize_text(value, limit) if isinstance(value, str) else ""


def _run_session(run: Any, state: str, project: Stamped[str]) -> Session | None:
    if not isinstance(run, dict):
        return None
    rid = run.get("id")
    if not isinstance(rid, int) or isinstance(rid, bool):
        return None
    actor = run.get("actor")
    path = run.get("path")
    if not (
        run.get("event") == "dynamic"
        and isinstance(actor, dict)
        and actor.get("login") == "Copilot"
        and isinstance(path, str)
        and path.startswith("dynamic/")
    ):
        return None
    kind = "review" if path == _REVIEW_PATH else "session (unverified)"
    name = _str(run.get("name"), _NAME_LEN)
    return Session(
        pid=0,
        agent="copilot",
        session_id=f"run-{rid}",
        project=project,
        project_rule="api",
        task=sanitize_text(f"{kind}: {name}", _TASK_LEN),
        state=state,
        branch=_str(run.get("head_branch"), 100),
        worktree="",
        started=_epoch(run.get("run_started_at")),
        cpu=0.0,
        rss=0,
    )


def _task_branch(task: dict) -> str:
    arts = task.get("artifacts")
    if not isinstance(arts, list):
        return ""
    for art in arts[:20]:
        if not isinstance(art, dict) or art.get("type") != "branch":
            continue
        data = art.get("data")
        src = data if isinstance(data, dict) else art
        ref = src.get("head_ref")
        if isinstance(ref, str):
            return sanitize_text(ref, 100)
    return ""


def parse_agent_tasks(payload: Any, now: float | None = None) -> list[Session]:
    """Map an Agent tasks list payload to rows. Terminal and unknown states are dropped."""
    stamp = time.time() if now is None else now
    if isinstance(payload, dict):
        payload = payload.get("tasks")
    if not isinstance(payload, list):
        return []
    out: list[Session] = []
    for task in payload[:_SCAN_CAP]:
        if len(out) >= MAX_TASKS:
            break
        if not isinstance(task, dict):
            continue
        tid = task.get("id")
        state = _TASK_STATE.get(task.get("state")) if isinstance(task.get("state"), str) else None
        if not isinstance(tid, str) or not tid or state is None:
            continue
        sid = sanitize_text(tid, _ID_LEN)
        if not sid:
            continue
        out.append(
            Session(
                pid=0,
                agent="copilot",
                session_id=f"task-{sid}",
                project=Stamped(_repo_name(task.get("repo", "")), stamp),
                project_rule="api",
                task=sanitize_text(f"agent task: {_str(task.get('name'), _NAME_LEN)}", _TASK_LEN),
                state=state,
                branch=_task_branch(task),
                worktree="",
                started=_epoch(task.get("created_at")),
                cpu=0.0,
                rss=0,
            )
        )
    return out


class CopilotSource:
    name = "copilot"

    def __init__(
        self,
        runs_api: RunsApi | None,
        repos: list[str],
        agent_tasks: AgentTasksApi | None = None,
    ) -> None:
        self._runs_api = runs_api
        self._repos = list(repos)
        self._agent_tasks = agent_tasks

    def collect(self, ctx: Ctx) -> SourceResult:
        now = ctx.clock()
        sessions: list[Session] = []
        if self._runs_api is not None:
            for repo in self._repos:
                sessions.extend(self._repo_sessions(repo, now))
        if self._agent_tasks is not None:
            sessions.extend(parse_agent_tasks(self._agent_tasks.tasks(), now=now))
        return SourceResult(sessions=sessions)

    def _repo_sessions(self, repo: str, now: float) -> list[Session]:
        assert self._runs_api is not None
        project = Stamped(_repo_name(repo), now)
        seen: set[str] = set()
        rows: list[Session] = []
        for status, state in _RUN_STATUS_STATE.items():
            runs = self._runs_api.runs(repo, status)
            if not isinstance(runs, list):
                raise TypeError("runs api returned a non-list")
            for run in runs[:_SCAN_CAP]:
                if len(rows) >= MAX_RUNS_PER_REPO:
                    return rows
                row = _run_session(run, state, project)
                if row is not None and row.session_id not in seen:
                    seen.add(row.session_id)
                    rows.append(row)
        return rows
