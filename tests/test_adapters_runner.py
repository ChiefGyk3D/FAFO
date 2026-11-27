"""Real-subprocess tests of the default runner: deadline, byte cap, process-group kill."""

import os
import signal
import sys
import threading
import time

from fafo.adapters.safo import MAX_OUTPUT, SafoAdapter

GOOD = '{"sessions": [{"session_id": "s1", "issue": "o/r#1"}]}'


def child(code):
    return [sys.executable, "-c", code]


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_gone(pids, limit=2.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if not any(pid_alive(p) for p in pids):
            return True
        time.sleep(0.02)
    return False


def run_guarded(adapter, pidfile, limit):
    """Run enrich in a thread so a regression fails instead of hanging the suite."""
    result = {}

    def target():
        result["out"] = adapter.enrich(["s1"], {})

    t0 = time.monotonic()
    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(limit)
    elapsed = time.monotonic() - t0
    hung = th.is_alive()
    pids = []
    if pidfile.exists():
        pids = [int(x) for x in pidfile.read_text().split()]
    if hung:  # clean up the stuck child so the failing run leaves nothing behind
        for pid in pids[:1]:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        th.join(2)
    return result.get("out"), elapsed, hung, pids


def test_silent_hang_times_out_stale_and_child_gone(tmp_path):
    pf = tmp_path / "pids"
    code = f"import os,time;open({str(pf)!r},'w').write(str(os.getpid()));time.sleep(30)"
    a = SafoAdapter(child(code), timeout_s=0.5)
    out, elapsed, hung, pids = run_guarded(a, pf, 3.0)
    assert not hung, "runner blocked past its deadline"
    assert out == {} and a.health == "stale"
    assert elapsed < 1.5
    assert pids and wait_gone(pids)
    try:
        os.killpg(pids[0], 0)
        group_left = True
    except ProcessLookupError:
        group_left = False
    assert not group_left


def test_megabyte_output_cut_at_cap_and_killed(tmp_path):
    pf = tmp_path / "pids"
    code = (
        f"import os,sys,time;open({str(pf)!r},'w').write(str(os.getpid()));"
        "sys.stdout.write('x'*1000000);sys.stdout.flush();time.sleep(30)"
    )
    a = SafoAdapter(child(code), timeout_s=3.0)
    out, elapsed, hung, pids = run_guarded(a, pf, 4.0)
    assert not hung
    assert out == {} and a.health == "absent"
    assert elapsed < 2.5
    assert pids and wait_gone(pids)


def test_slow_drip_cut_by_deadline(tmp_path):
    pf = tmp_path / "pids"
    code = (
        f"import os,sys,time;open({str(pf)!r},'w').write(str(os.getpid()))\n"
        "for _ in range(100):\n"
        "    sys.stdout.write(' ');sys.stdout.flush();time.sleep(0.1)\n"
    )
    a = SafoAdapter(child(code), timeout_s=0.5)
    out, elapsed, hung, pids = run_guarded(a, pf, 3.0)
    assert not hung
    assert out == {} and a.health == "stale"
    assert elapsed < 1.5
    assert pids and wait_gone(pids)


def test_grandchild_killed_with_group(tmp_path):
    pf = tmp_path / "pids"
    code = (
        "import os,subprocess,sys,time\n"
        "g=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
        f"open({str(pf)!r},'w').write(str(os.getpid())+' '+str(g.pid))\n"
        "time.sleep(30)\n"
    )
    a = SafoAdapter(child(code), timeout_s=0.7)
    _out, _elapsed, hung, pids = run_guarded(a, pf, 3.0)
    assert not hung
    assert a.health == "stale" and len(pids) == 2
    assert wait_gone(pids)


def test_fast_json_child_still_works(tmp_path):
    a = SafoAdapter(child(f"print({GOOD!r})"), timeout_s=3.0)
    out, _elapsed, hung, _ = run_guarded(a, tmp_path / "none", 4.0)
    assert not hung and a.health == "ok"
    assert out["s1"].issue == "o/r#1"
    assert MAX_OUTPUT > 1000


def test_normal_exit_kills_group_once_before_reap(group_kill_spy):
    from fafo.adapters.safo import _default_runner

    raw = _default_runner(child(f"print({GOOD!r})"), 3.0, {"PATH": os.environ["PATH"]})
    assert GOOD.encode() in raw
    kills = group_kill_spy.kills()
    assert len(kills) == 1
    assert group_kill_spy.events[0] == kills[0]  # no wait before the kill
    assert group_kill_spy.reaped_when_killed == []


def test_normal_exit_leaves_no_grandchild(tmp_path):
    pf = tmp_path / "pids"
    code = (
        "import subprocess,sys\n"
        "g=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        f"open({str(pf)!r},'w').write(str(g.pid))\n"
        f"print({GOOD!r})\n"
    )
    a = SafoAdapter(child(code), timeout_s=3.0)
    out, _elapsed, hung, _ = run_guarded(a, tmp_path / "none", 4.0)
    assert not hung and a.health == "ok" and out["s1"].issue == "o/r#1"
    assert wait_gone([int(pf.read_text())])
