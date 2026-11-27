"""Real-subprocess tests of the default runner: deadline, byte cap, process-group kill.

Children are /bin/sh scripts that record their pid(s) as the first action, so they are ready in
a few milliseconds even on a loaded machine. Tests that assert post-kill state use a generous
deadline; deadline accuracy is tested separately by test_deadline_is_accurate with a trivial
child. A test never asserts on child start-up time.
"""

import os
import signal
import threading
import time

from conftest import Ran, deadline_bound, run_when_ready, wait_gone, wait_group_gone

from fafo.adapters.safo import MAX_OUTPUT, SafoAdapter

GOOD = '{"sessions": [{"session_id": "s1", "issue": "o/r#1"}]}'
SH = "/bin/sh"
# Roomy deadline for tests that assert state AFTER the kill (the child must have started).
DEADLINE = 3.0


def child(body, *args):
    """A /bin/sh -c child; $0 is "sh" and the extra args are $1, $2, ..."""
    return [SH, "-c", body, "sh", *map(str, args)]


def run_guarded(adapter, pidfile, limit):
    """Run enrich in a thread so a regression fails instead of hanging the suite.

    Returns (out, elapsed, hung). elapsed covers only the enrich() call itself.
    """
    result = {}

    def target():
        t0 = time.monotonic()
        result["out"] = adapter.enrich(["s1"], {})
        result["elapsed"] = time.monotonic() - t0

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(limit)
    hung = th.is_alive()
    if hung:  # clean up the stuck child so the failing run leaves nothing behind
        if pidfile.exists() and pidfile.read_text().split():
            try:
                os.killpg(int(pidfile.read_text().split()[0]), signal.SIGKILL)
            except ProcessLookupError:
                pass
        th.join(2)
    return result.get("out"), result.get("elapsed"), hung


def test_silent_hang_times_out_stale_and_child_gone(tmp_path):
    def scenario(deadline):
        pf = tmp_path / f"pids-{deadline}"
        a = SafoAdapter(child('echo $$ > "$1"; exec sleep 30', pf), timeout_s=deadline)
        out, elapsed, hung = run_guarded(a, pf, deadline + 10)
        return Ran((a, out, elapsed, hung), pf)

    r = run_when_ready(scenario)
    a, out, elapsed, hung = r.result
    assert not hung, "runner blocked past its deadline"
    assert out == {} and a.health == "stale"
    assert elapsed < deadline_bound(r.deadline)
    assert wait_gone(r.pids), "child survived the kill"
    assert wait_group_gone(r.pids[0]), "process group still exists after the kill"


def test_deadline_is_accurate(tmp_path):
    """The one tight timing test: a trivial child, deadline <= elapsed < deadline + slack."""
    a = SafoAdapter(child("exec sleep 30"), timeout_s=1.0)
    out, elapsed, hung = run_guarded(a, tmp_path / "none", 15.0)
    assert not hung
    assert out == {} and a.health == "stale"
    assert 1.0 <= elapsed < deadline_bound(1.0)


def test_megabyte_output_cut_at_cap_and_killed(tmp_path):
    body = 'echo $$ > "$1"; head -c 1000000 /dev/zero | tr "\\0" x; exec sleep 30'

    def scenario(deadline):
        pf = tmp_path / f"pids-{deadline}"
        a = SafoAdapter(child(body, pf), timeout_s=max(deadline, 5.0))
        out, elapsed, hung = run_guarded(a, pf, deadline + 15)
        return Ran((a, out, elapsed, hung), pf)

    r = run_when_ready(scenario)
    a, out, elapsed, hung = r.result
    assert not hung
    assert out == {} and a.health == "absent"  # absent = cut by the byte cap, not the deadline
    assert elapsed < deadline_bound(max(r.deadline, 5.0))
    assert wait_gone(r.pids)


def test_slow_drip_cut_by_deadline(tmp_path):
    body = 'echo $$ > "$1"; while :; do printf " "; sleep 0.1; done'

    def scenario(deadline):
        pf = tmp_path / f"pids-{deadline}"
        a = SafoAdapter(child(body, pf), timeout_s=deadline)
        out, elapsed, hung = run_guarded(a, pf, deadline + 10)
        return Ran((a, out, elapsed, hung), pf)

    r = run_when_ready(scenario)
    a, out, elapsed, hung = r.result
    assert not hung
    assert out == {} and a.health == "stale"
    assert elapsed < deadline_bound(r.deadline)
    assert wait_gone(r.pids), "drip child survived the kill"
    assert wait_group_gone(r.pids[0]), "process group still exists after the kill"


def test_grandchild_killed_with_group(tmp_path):
    def scenario(deadline):
        pf = tmp_path / f"pids-{deadline}"
        # The grandchild is started first, then both pids are written in one line.
        a = SafoAdapter(child('sleep 30 & echo "$$ $!" > "$1"; wait', pf), timeout_s=deadline)
        out, elapsed, hung = run_guarded(a, pf, deadline + 10)
        return Ran((a, out, elapsed, hung), pf)

    r = run_when_ready(scenario)
    a, _out, elapsed, hung = r.result
    assert not hung
    assert a.health == "stale" and len(r.pids) == 2
    assert elapsed < deadline_bound(r.deadline)
    assert wait_gone(r.pids), "child or grandchild survived the group kill"


def test_fast_json_child_still_works(tmp_path):
    a = SafoAdapter(child('printf %s "$1"', GOOD), timeout_s=10.0)
    out, _elapsed, hung = run_guarded(a, tmp_path / "none", 15.0)
    assert not hung and a.health == "ok"
    assert out["s1"].issue == "o/r#1"
    assert MAX_OUTPUT > 1000


def test_normal_exit_kills_group_once_before_reap(group_kill_spy):
    from fafo.adapters.safo import _default_runner

    raw = _default_runner(child('printf %s "$1"', GOOD), 10.0, {"PATH": os.environ["PATH"]})
    assert GOOD.encode() in raw
    kills = group_kill_spy.kills()
    assert len(kills) == 1
    assert group_kill_spy.events[0] == kills[0]  # no wait before the kill
    assert group_kill_spy.reaped_when_killed == []


def test_normal_exit_leaves_no_grandchild(tmp_path):
    def scenario(deadline):
        pf = tmp_path / f"pids-{deadline}"
        body = 'sleep 30 >/dev/null 2>&1 & echo $! > "$1"; printf %s "$2"'
        a = SafoAdapter(child(body, pf, GOOD), timeout_s=max(deadline, 10.0))
        out, _elapsed, hung = run_guarded(a, tmp_path / "none", deadline + 15)
        return Ran((a, out, hung), pf)

    r = run_when_ready(scenario)
    a, out, hung = r.result
    assert not hung and a.health == "ok" and out["s1"].issue == "o/r#1"
    assert wait_gone(r.pids), "grandchild survived a normal exit"
