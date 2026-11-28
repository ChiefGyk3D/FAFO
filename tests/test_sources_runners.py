"""Tests for the runners source: fake GitHub client and fake ssh, no network."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from fafo.config import SourceCfg
from fafo.ghapi import RateLimited
from fafo.source import Ctx
from fafo.sources.runners import RunnerHost, RunnersCfg, RunnersSource
from fafo.sshro import SshError, SshHost

START = datetime(2026, 11, 27, 10, 0, 0, tzinfo=UTC).timestamp()
NOW = START + 130
ISO = "2026-11-27T10:00:00Z"
CTX = Ctx(cfg=SourceCfg(), clock=lambda: NOW, log=logging.getLogger("test"))
ESC = chr(27)

LOADAVG = "0.50 0.40 0.30 1/200 999\n"
MEMINFO = "MemTotal:       1000 kB\nMemFree: 100 kB\nMemAvailable:    250 kB\n"
DF = "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/x 1000 420 580 42% /\n"
WORKERS = "123 55 Runner.Listener /x/bin/Runner.Listener run\n"


class FakeGh:
    def __init__(self, runs=None, jobs=None, exc=None):
        self._runs = runs or {}
        self._jobs = jobs or {}
        self.exc = exc
        self.run_calls: list[tuple] = []
        self.job_calls: list[tuple] = []

    def runs(self, repo, status):
        self.run_calls.append((repo, status))
        if self.exc:
            raise self.exc
        return list(self._runs.get((repo, status), []))

    def jobs(self, repo, run_id):
        self.job_calls.append((repo, run_id))
        return list(self._jobs.get((repo, run_id), []))


def steps(done: int, total: int):
    out = []
    for i in range(1, total + 1):
        st = "completed" if i <= done else ("in_progress" if i == done + 1 else "queued")
        out.append({"name": f"step{i}", "status": st, "number": i})
    return out


def job(name="build", runner="runner-a", status="in_progress", st=None, started=ISO):
    return {
        "name": name,
        "status": status,
        "runner_name": runner,
        "started_at": started,
        "steps": st if st is not None else steps(2, 5),
    }


def good_ssh(host, op, timeout_s=6.0):
    return {"loadavg": LOADAVG, "meminfo": MEMINFO, "df": DF, "workers": WORKERS}[op]


def cfg(repos=("o/r",), runners=None, token_env="T"):
    runners = runners or [RunnerHost("runner-a", SshHost("runner-a"), None)]
    return RunnersCfg(repos=list(repos), runners=runners, token_env=token_env)


def run_of(rid=1, name="CI"):
    return {"id": rid, "name": name}


def collect(src):
    return src.collect(CTX)


def test_source_name():
    assert RunnersSource(cfg(), None).name == "runners"


def test_job_maps_to_runner_by_name_with_step_and_elapsed():
    gh = FakeGh({("o/r", "in_progress"): [run_of(1, "CI")]}, {("o/r", 1): [job()]})
    res = collect(RunnersSource(cfg(), gh, ssh=good_ssh))
    (r,) = res.runners
    assert (r.name, r.state, r.repo, r.workflow, r.job) == ("runner-a", "busy", "o/r", "CI", "build")
    assert (r.step, r.step_n, r.step_total) == ("step3", 3, 5)
    assert r.elapsed == pytest.approx(130)
    assert r.load == pytest.approx(0.5)
    assert r.mem == pytest.approx(75.0)
    assert r.disk == pytest.approx(42.0)


def test_runner_with_no_job_is_idle():
    gh = FakeGh()
    (r,) = collect(RunnersSource(cfg(), gh, ssh=good_ssh)).runners
    assert r.state == "idle" and r.job == "" and r.step_total == 0 and r.elapsed == 0.0


def test_unmatched_jobs_are_ignored():
    gh = FakeGh(
        {("o/r", "in_progress"): [run_of(1)]},
        {("o/r", 1): [job(runner="someone-else"), job(runner=None), job(status="completed")]},
    )
    (r,) = collect(RunnersSource(cfg(), gh, ssh=good_ssh)).runners
    assert r.state == "idle"


def test_one_row_per_configured_runner_and_repo_filter():
    runners = [
        RunnerHost("runner-a", None, None),
        RunnerHost("runner-b", None, ["o/other"]),
    ]
    gh = FakeGh(
        {("o/r", "in_progress"): [run_of(1)]},
        {("o/r", 1): [job(runner="runner-a"), job(name="x", runner="runner-b")]},
    )
    rows = {r.name: r for r in collect(RunnersSource(cfg(runners=runners), gh)).runners}
    assert set(rows) == {"runner-a", "runner-b"}
    assert rows["runner-a"].state == "busy"
    assert rows["runner-b"].state == "idle"  # its repos list excludes o/r


def test_queued_and_in_progress_runs_both_requested_and_deduped():
    same = run_of(1)
    gh = FakeGh(
        {("o/r", "in_progress"): [same], ("o/r", "queued"): [same, run_of(2)]},
        {("o/r", 1): [job()]},
    )
    collect(RunnersSource(cfg(), gh, ssh=good_ssh))
    assert {s for _, s in gh.run_calls} == {"in_progress", "queued"}
    assert sorted(rid for _, rid in gh.job_calls) == [1, 2]


def test_cap_runs_per_repo():
    many = [run_of(i) for i in range(1, 60)]
    gh = FakeGh({("o/r", "in_progress"): many})
    collect(RunnersSource(cfg(), gh, ssh=good_ssh))
    assert len(gh.job_calls) == 20


def test_cap_jobs_total_at_100():
    runs = [run_of(i) for i in range(1, 21)]
    many_jobs = {("o/r", i): [job(name=f"j{i}-{n}", runner="other") for n in range(30)] for i in range(1, 21)}
    gh = FakeGh({("o/r", "in_progress"): runs}, many_jobs)
    src = RunnersSource(cfg(), gh, ssh=good_ssh)
    collect(src)
    assert len(gh.job_calls) <= 4  # 4 runs x 30 jobs reaches 100 and stops fetching
    # a runner job beyond the 100th job seen is not used
    late = {("o/r", i): [job(name="x", runner="other") for _ in range(50)] for i in (1, 2)}
    late[("o/r", 3)] = [job(name="late", runner="runner-a")]
    gh2 = FakeGh({("o/r", "in_progress"): [run_of(1), run_of(2), run_of(3)]}, late)
    (r,) = collect(RunnersSource(cfg(), gh2, ssh=good_ssh)).runners
    assert r.state == "idle"


def test_step_counts_when_all_steps_done_or_missing():
    gh = FakeGh(
        {("o/r", "in_progress"): [run_of(1)]},
        {("o/r", 1): [job(st=steps(3, 3))]},
    )
    (r,) = collect(RunnersSource(cfg(), gh)).runners
    assert (r.state, r.step, r.step_n, r.step_total) == ("busy", "", 0, 3)
    gh = FakeGh({("o/r", "in_progress"): [run_of(1)]}, {("o/r", 1): [job(st=[])]})
    (r,) = collect(RunnersSource(cfg(), gh)).runners
    assert (r.step, r.step_n, r.step_total) == ("", 0, 0)


def test_ssh_failure_gives_unknown_host_fields_and_note_not_failure():
    def bad(host, op, timeout_s=6.0):
        raise SshError("ssh connection, host key or auth failure (exit 255)")

    gh = FakeGh({("o/r", "in_progress"): [run_of(1)]}, {("o/r", 1): [job()]})
    src = RunnersSource(cfg(), gh, ssh=bad)
    res = collect(src)
    (r,) = res.runners
    assert r.state == "busy" and (r.load, r.mem, r.disk) == (-1.0, -1.0, -1.0)
    assert res.status in (None, "ok")
    assert "runner-a" in src.note and "ssh" in src.note


def test_no_listener_over_ssh_means_down():
    def ssh(host, op, timeout_s=6.0):
        return "" if op == "workers" else good_ssh(host, op)

    (r,) = collect(RunnersSource(cfg(), FakeGh(), ssh=ssh)).runners
    assert r.state == "down"


def test_rate_limited_propagates_so_collector_keeps_old_rows():
    gh = FakeGh(exc=RateLimited(NOW + 90))
    src = RunnersSource(cfg(), gh, ssh=good_ssh)
    with pytest.raises(RateLimited) as ei:
        collect(src)
    assert ei.value.not_before == NOW + 90


def test_strings_are_sanitized_and_capped():
    nasty = ESC + "]0;pwn" + chr(7) + "evil" + "A" * 500
    gh = FakeGh(
        {("o/r", "in_progress"): [run_of(1, nasty)]},
        {("o/r", 1): [job(name=nasty, st=[{"name": nasty, "status": "in_progress", "number": 1}])]},
    )
    (r,) = collect(RunnersSource(cfg(), gh)).runners
    for s in (r.workflow, r.job, r.step):
        assert ESC not in s and chr(7) not in s and "pwn" not in s and len(s) <= 200


def test_garbage_job_fields_do_not_crash():
    bad = {"name": 5, "status": "in_progress", "runner_name": "runner-a", "started_at": "nope",
           "steps": "x"}
    gh = FakeGh({("o/r", "in_progress"): [run_of(1)]}, {("o/r", 1): [bad]})
    (r,) = collect(RunnersSource(cfg(), gh)).runners
    assert r.state == "busy" and r.elapsed == 0.0


def test_gh_none_still_reports_host_state():
    (r,) = collect(RunnersSource(cfg(), None, ssh=good_ssh)).runners
    assert r.state == "idle" and r.load == pytest.approx(0.5)


def _busy(job_steps):
    gh = FakeGh({("o/r", "in_progress"): [run_of(1)]}, {("o/r", 1): [job(st=job_steps)]})
    (r,) = collect(RunnersSource(cfg(), gh)).runners
    return r


def test_invalid_step_numbers_are_ignored_at_the_parse_boundary():
    st = [
        {"name": "a", "status": "completed", "number": 1},
        {"name": "neg", "status": "in_progress", "number": -1},
        {"name": "bool", "status": "queued", "number": True},
        {"name": "str", "status": "queued", "number": "3"},
        {"name": "float", "status": "queued", "number": 2.5},
        {"name": "none", "status": "queued"},
        {"name": "e", "status": "queued", "number": 3},
    ]
    r = _busy(st)
    assert (r.step, r.step_n, r.step_total) == ("e", 3, 2)
    assert r.step_n >= 0


def test_all_steps_invalid_gives_empty_step():
    r = _busy([{"name": "x", "status": "in_progress", "number": -5}])
    assert (r.step, r.step_n, r.step_total) == ("", 0, 0)


@pytest.mark.parametrize(
    "bad",
    [
        {"loadavg": "nan 0 0 1/2 3\n"},
        {"loadavg": "inf 0 0 1/2 3\n"},
        {"loadavg": "-1.0 0 0 1/2 3\n"},
        {"df": "Filesystem 1 2 3 Capacity M\n/dev/x 1 2 3 nan% /\n"},
        {"df": "Filesystem 1 2 3 Capacity M\n/dev/x 1 2 3 inf% /\n"},
        {"meminfo": "MemTotal: 0 kB\nMemAvailable: 0 kB\n"},
    ],
)
def test_non_finite_ssh_values_are_a_parse_failure(bad):
    def ssh(host, op, timeout_s=6.0):
        return bad.get(op, good_ssh(host, op))

    src = RunnersSource(cfg(), FakeGh(), ssh=ssh)
    (r,) = collect(src).runners
    assert (r.load, r.mem, r.disk) == (-1.0, -1.0, -1.0)
    assert "runner-a" in src.note


PS_SELF_ONLY = (
    " 4321       0 grep            grep -E [R]unner.Worker|[R]unner.Listener\n"
    " 4320       0 sh              sh -c ps -eo pid | grep -E Runner.Worker|Runner.Listener\n"
    " 4319       0 ps              ps -eo pid,etimes,comm,args --no-headers\n"
)


def _ssh_with_ps(ps):
    def ssh(host, op, timeout_s=6.0):
        return ps if op == "workers" else good_ssh(host, op)

    return ssh


def test_ps_self_matches_do_not_count_as_a_listener():
    (r,) = collect(RunnersSource(cfg(), FakeGh(), ssh=_ssh_with_ps(PS_SELF_ONLY))).runners
    assert r.state == "down"


def test_real_listener_and_worker_lines_are_recognized():
    ps = PS_SELF_ONLY + (
        " 100  9000 Runner.Listener /r/bin/Runner.Listener run\n"
        " 101    50 Runner.Worker   /r/bin/Runner.Worker spawnclient 1 2\n"
    )
    (r,) = collect(RunnersSource(cfg(), FakeGh(), ssh=_ssh_with_ps(ps))).runners
    assert r.state == "busy"
    ps = PS_SELF_ONLY + " 100  9000 Runner.Listener /r/bin/Runner.Listener run\n"
    (r,) = collect(RunnersSource(cfg(), FakeGh(), ssh=_ssh_with_ps(ps))).runners
    assert r.state == "idle"


def test_credential_shaped_workflow_job_and_step_names_are_masked():
    secret = "Zm9vYmFyYmF6" * 6
    st = [{"name": "Authorization: Bearer abc", "status": "in_progress", "number": 1}]
    gh = FakeGh(
        {("o/r", "in_progress"): [run_of(1, "token=hunter2")]},
        {("o/r", 1): [job(name=f"deploy {secret}", st=st)]},
    )
    (r,) = collect(RunnersSource(cfg(), gh, ssh=good_ssh)).runners
    blob = repr(r)
    for needle in ("hunter2", secret, "Bearer abc"):
        assert needle not in blob, needle
    assert "<redacted>" in r.workflow and "<redacted>" in r.job


def test_benign_runner_labels_unchanged():
    gh = FakeGh({("o/r", "in_progress"): [run_of(1, "CI")]}, {("o/r", 1): [job()]})
    (r,) = collect(RunnersSource(cfg(), gh, ssh=good_ssh)).runners
    assert (r.workflow, r.job, r.step) == ("CI", "build", "step3")
