"""Runners source: CI runner activity from the Actions jobs API plus host load over read-only ssh.

One `Runner` row per configured runner. Activity comes from jobs whose `runner_name` matches
a configured runner (the runners-list endpoints need Administration read and are not used).
Host fields (load, memory percent, disk percent) come from the fixed ssh ops; they are -1.0
when unknown. An ssh failure is a note in `note` and unknown host fields, not a failed source.
A GitHub rate limit (or any GitHub error) is raised unchanged, so the collector keeps the
previous rows and reports the source stale or down.

Row state: busy (an in-progress job matched, or a Runner.Worker process is running), down
(ssh worked but no Runner.Listener process exists) or idle.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fafo.ghapi import GhClient
from fafo.model import Runner
from fafo.redact import mask_credentials, sanitize_text
from fafo.source import Ctx, SourceResult
from fafo.sshro import SshError, SshHost, ssh_ro

MAX_RUNS_PER_REPO = 20
MAX_JOBS_TOTAL = 100
TEXT_MAX = 200
UNKNOWN = -1.0
_RUN_STATUSES = ("in_progress", "queued")


@dataclass
class RunnerHost:
    name: str
    ssh: SshHost | None = None
    repos: list[str] | None = None


@dataclass
class RunnersCfg:
    repos: list[str] = field(default_factory=list)
    runners: list[RunnerHost] = field(default_factory=list)
    token_env: str = "GITHUB_TOKEN"


def _txt(value: object) -> str:
    """API-supplied display text: credential-masked before the length cap."""
    return sanitize_text(mask_credentials(value), TEXT_MAX) if isinstance(value, str) else ""


def _ident(value: object) -> str:
    """Configured names (runner host, repo) identify a row: sanitized, never masked, so two
    distinct names cannot collapse into one."""
    return sanitize_text(value, TEXT_MAX) if isinstance(value, str) else ""


def _epoch(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _valid_step(step: object) -> bool:
    """A step is usable only if its number is a real int (not bool) >= 0."""
    if not isinstance(step, dict):
        return False
    n = step.get("number")
    return type(n) is int and n >= 0


def _step_info(job: dict) -> tuple[str, int, int]:
    raw = job.get("steps")
    if not isinstance(raw, list):
        return "", 0, 0
    steps = [s for s in raw if _valid_step(s)]
    for s in steps:
        if s.get("status") != "completed":
            return _txt(s.get("name")), s["number"], len(steps)
    return "", 0, len(steps)


def _finite(value: float) -> float:
    if not math.isfinite(value) or value < 0:
        raise ValueError("non-finite or negative metric")
    return value


def _parse_load(text: str) -> float:
    return _finite(float(text.split()[0]))


def _parse_mem(text: str) -> float:
    vals: dict[str, int] = {}
    for line in text.splitlines():
        m = re.match(r"(MemTotal|MemAvailable):\s+(\d+)", line)
        if m:
            vals[m[1]] = int(m[2])
    total, avail = vals["MemTotal"], vals["MemAvailable"]
    if total <= 0:
        raise ValueError("no memory total")
    return _finite(round(100.0 * (1 - min(avail, total) / total), 1))


def _parse_disk(text: str) -> float:
    cols = text.strip().splitlines()[-1].split()
    return _finite(float(cols[4].rstrip("%")))


def _scan_procs(text: str) -> tuple[bool, bool]:
    """(listener, worker) from `ps -eo pid,etimes,comm,args` lines, judged by the comm column.

    ps, grep and shell lines that merely mention the runner names are never counted.
    """
    listener = worker = False
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        comm = parts[2]
        if comm == "Runner.Listener":
            listener = True
        elif comm == "Runner.Worker":
            worker = True
    return listener, worker


class RunnersSource:
    name = "runners"

    def __init__(self, cfg: RunnersCfg, gh: GhClient | None, ssh=ssh_ro) -> None:
        self.cfg = cfg
        self.gh = gh
        self._ssh = ssh
        self.note = ""

    # -- GitHub side -------------------------------------------------------------------------

    def _active_jobs(self) -> list[tuple[str, str, dict]]:
        """(repo, workflow name, job) for in-progress jobs, within the run and job caps."""
        out: list[tuple[str, str, dict]] = []
        if self.gh is None:
            return out
        seen_jobs = 0
        for repo in self.cfg.repos:
            runs: dict[int, str] = {}
            for status in _RUN_STATUSES:
                for run in self.gh.runs(repo, status)[:MAX_RUNS_PER_REPO]:
                    rid = run.get("id")
                    if type(rid) is int and rid not in runs and len(runs) < MAX_RUNS_PER_REPO:
                        runs[rid] = _txt(run.get("name"))
            for rid, wf in runs.items():
                if seen_jobs >= MAX_JOBS_TOTAL:
                    return out
                jobs = self.gh.jobs(repo, rid)[: MAX_JOBS_TOTAL - seen_jobs]
                seen_jobs += len(jobs)
                out.extend((repo, wf, j) for j in jobs if j.get("status") == "in_progress")
        return out

    # -- ssh side ----------------------------------------------------------------------------

    def _host(self, host: RunnerHost, notes: list[str]) -> tuple[float, float, float, bool | None, bool]:
        """(load, mem, disk, listener present or None if unknown, worker present)."""
        if host.ssh is None:
            return UNKNOWN, UNKNOWN, UNKNOWN, None, False
        load = mem = disk = UNKNOWN
        listener: bool | None = None
        worker = False
        try:
            load = _parse_load(self._ssh(host.ssh, "loadavg"))
            mem = _parse_mem(self._ssh(host.ssh, "meminfo"))
            disk = _parse_disk(self._ssh(host.ssh, "df"))
            procs = self._ssh(host.ssh, "workers")
            listener, worker = _scan_procs(procs)
        except SshError as exc:
            notes.append(f"{_txt(host.name)}: ssh {_txt(str(exc))}")
        except (ValueError, KeyError, IndexError):
            notes.append(f"{_txt(host.name)}: ssh output not understood")
        if notes and notes[-1].startswith(f"{_txt(host.name)}:"):
            load = mem = disk = UNKNOWN
            listener, worker = None, False
        return load, mem, disk, listener, worker

    # -- collect -----------------------------------------------------------------------------

    def collect(self, ctx: Ctx) -> SourceResult:
        now = ctx.clock()
        active = self._active_jobs()  # may raise RateLimited or GhError: rows are kept
        notes: list[str] = []
        rows: list[Runner] = []
        for host in self.cfg.runners:
            match = next(
                (
                    (repo, wf, j)
                    for repo, wf, j in active
                    if j.get("runner_name") == host.name
                    and (host.repos is None or repo in host.repos)
                ),
                None,
            )
            load, mem, disk, listener, worker = self._host(host, notes)
            repo = wf = jobname = step = ""
            step_n = step_total = 0
            elapsed = 0.0
            if match is not None:
                repo, wf, j = match
                repo = _ident(repo)
                jobname = _txt(j.get("name"))
                step, step_n, step_total = _step_info(j)
                started = _epoch(j.get("started_at"))
                elapsed = max(0.0, now - started) if started is not None else 0.0
            if listener is False:
                state = "down"
            elif match is not None or worker:
                state = "busy"
            else:
                state = "idle"
            rows.append(
                Runner(
                    name=_ident(host.name),
                    state=state,
                    repo=repo,
                    workflow=wf,
                    job=jobname,
                    step=step,
                    step_n=step_n,
                    step_total=step_total,
                    elapsed=elapsed,
                    load=load,
                    mem=mem,
                    disk=disk,
                )
            )
        self.note = sanitize_text("; ".join(notes), 500)
        if self.note:
            ctx.log.warning("runners degraded: %s", self.note)
        return SourceResult(runners=rows)
