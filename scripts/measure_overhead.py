"""Measure the FAFO collector's CPU, memory and display continuity in a child process.

The collector (tests/perf/stubs.py, a real Collector with stub sources) runs as its own
process, so the CPU and RSS read through psutil are the collector's alone.

Phases:
  idle    7 sessions x 20 children, static rows        budget: CPU < 2 percent of one core
  active  7 sessions x 256 children, rows change       budget: CPU < 5 percent of one core
  stress  busy-loop processes plus a source that hangs forever; the snapshot and views.json
          must keep updating, the hung source must read stale or down with its rows kept
All phases: resident memory < 100 MB.

CONTENTION ALLOWANCE: under stress the CPU budget is 2x the idle budget (4 percent). CPU is the
collector's own cpu_times() delta, not wall clock, so starvation does not inflate it, but cache
and scheduler contention from the busy loops can raise the cost of the same work.

Usage: python scripts/measure_overhead.py [--phase idle|active|stress|all] [--window S]
       [--loops N]    (window up to 120 s, loops up to 8)
Prints one JSON line per phase; appends a markdown table to $GITHUB_STEP_SUMMARY when set.
Exit code 1 when a budget or continuity check fails.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
STUBS = ROOT / "tests" / "perf" / "stubs.py"
SRC = ROOT / "src"

TICK_S = 2.0  # the default tick: the smallest default source interval
READ_EVERY_S = 0.25
MAX_GAP_FACTOR = 3.0  # snapshot gap must stay under 3 ticks
IDLE_CPU_BUDGET = 2.0
ACTIVE_CPU_BUDGET = 5.0
STRESS_ALLOWANCE = 2.0  # contention allowance, see module docstring
STRESS_CPU_BUDGET = IDLE_CPU_BUDGET * STRESS_ALLOWANCE
RSS_BUDGET_MB = 100.0
MAX_WINDOW_S = 120.0
MAX_LOOPS = 8
HUNG_PID = 90001
READY_TIMEOUT_S = 30.0

_summary_header_done = False


def _gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def kill_by_pid(pid: int, proc: subprocess.Popen | None = None) -> None:
    """SIGKILL one pid we started, reap it, and verify it is gone."""
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if proc is not None:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    end = time.monotonic() + 10
    while not _gone(pid) and time.monotonic() < end:
        time.sleep(0.05)
    if not _gone(pid):
        raise RuntimeError(f"process {pid} still alive after SIGKILL")


def start_loops(n: int) -> list[subprocess.Popen]:
    return [
        subprocess.Popen(
            [sys.executable, "-c", "while True: pass"],
            start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(n)
    ]


def start_collector(workdir: Path, mode: str) -> subprocess.Popen:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(SRC),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.Popen(
        [sys.executable, str(STUBS), "--dir", str(workdir), "--mode", mode,
         "--tick", str(TICK_S)],
        env=env, cwd=str(ROOT), start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )


def _read_json(path: Path):
    """Return (document, ok). A missing file is (None, None); a parse failure (None, False)."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None, None
    try:
        return json.loads(raw), True
    except ValueError:
        return None, False


def _ready(snap, views_path: Path) -> bool:
    """Steady state: agents rows present and views.json written (the hung source is not awaited:
    a regression that blocks the writer must fail the gap check, not the start-up wait)."""
    return snap is not None and views_path.exists() and bool(snap.sessions)


def run_phase(phase: str, window_s: float, loops: int = 0) -> dict:
    """Run one phase and return the measured numbers plus a list of failures."""
    from fafo.render.view import build_views
    from fafo.store import read_snapshot

    window_s = min(float(window_s), MAX_WINDOW_S)
    loops = min(int(loops), MAX_LOOPS)
    mode = "stress" if phase == "stress" else phase
    workdir = Path(tempfile.mkdtemp(prefix="fafo-perf-"))
    snap_path = workdir / "snapshot.json"
    views_path = workdir / "views.json"
    busy: list[subprocess.Popen] = []
    child: subprocess.Popen | None = None
    failures: list[str] = []
    reads = parse_failures = 0
    stale_seen = False
    empty_views = 0
    max_gap = max_views_gap = 0.0
    cpu_pct = rss_mb = 0.0
    try:
        if phase == "stress":
            busy = start_loops(loops)
        child = start_collector(workdir, mode)
        ps = psutil.Process(child.pid)
        end = time.monotonic() + READY_TIMEOUT_S
        while not _ready(read_snapshot(snap_path), views_path):
            if child.poll() is not None:
                err = child.stderr.read().decode("utf-8", "replace")[-300:] if child.stderr else ""
                raise RuntimeError(f"collector exited early: {err}")
            if time.monotonic() > end:
                raise RuntimeError("collector never reached its steady state")
            time.sleep(0.1)

        cpu0 = ps.cpu_times()
        t0 = time.monotonic()
        last_taken = last_gen = None
        last_taken_at = last_gen_at = t0
        while True:
            now_m = time.monotonic()
            if now_m - t0 >= window_s:
                break
            _, ok = _read_json(snap_path)
            reads += 1
            if ok is False:
                parse_failures += 1
            snap = read_snapshot(snap_path)
            if snap is None:
                parse_failures += 1
            else:
                if last_taken is None or snap.taken > last_taken:
                    max_gap = max(max_gap, now_m - last_taken_at)
                    last_taken, last_taken_at = snap.taken, now_m
                views = build_views(snap, time.time())
                if views.status not in ("ok", "stale") or not views.sessions:
                    empty_views += 1
                if phase == "stress":
                    h = {x.name: x for x in snap.health}.get("hung")
                    has_rows = HUNG_PID in {s.pid for s in snap.sessions}
                    is_stale = h is not None and h.status in ("stale", "down")
                    if is_stale and has_rows:
                        stale_seen = True
                    if stale_seen:  # once seen stale, it must stay stale with its rows kept
                        if not is_stale:
                            failures.append("hung source left stale/down")
                        if not has_rows:
                            failures.append("hung source rows were dropped")
            vdoc, vok = _read_json(views_path)
            if vok is False:
                parse_failures += 1
            elif vdoc is not None:
                gen = vdoc.get("generated")
                if last_gen is None or gen != last_gen:
                    max_views_gap = max(max_views_gap, now_m - last_gen_at)
                    last_gen, last_gen_at = gen, now_m
            time.sleep(READ_EVERY_S)
        t1 = time.monotonic()
        # Count the open gap at the end of the window too: a stalled writer must not hide.
        max_gap = max(max_gap, t1 - last_taken_at)
        max_views_gap = max(max_views_gap, t1 - last_gen_at)
        cpu1 = ps.cpu_times()
        used = (cpu1.user + cpu1.system) - (cpu0.user + cpu0.system)
        cpu_pct = 100.0 * used / (t1 - t0)
        rss_mb = ps.memory_info().rss / (1024 * 1024)
    finally:
        if child is not None:
            try:
                os.kill(child.pid, signal.SIGTERM)
                child.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            kill_by_pid(child.pid, child)
            if child.stderr:
                child.stderr.close()
        for b in busy:
            kill_by_pid(b.pid, b)
        shutil.rmtree(workdir, ignore_errors=True)

    limit = {"idle": IDLE_CPU_BUDGET, "active": ACTIVE_CPU_BUDGET, "stress": STRESS_CPU_BUDGET}[phase]
    gap_limit = MAX_GAP_FACTOR * TICK_S
    if cpu_pct >= limit:
        failures.append(f"cpu {cpu_pct:.2f} percent over budget {limit}")
    if rss_mb >= RSS_BUDGET_MB:
        failures.append(f"rss {rss_mb:.1f} MB over budget {RSS_BUDGET_MB}")
    if parse_failures:
        failures.append(f"{parse_failures} parse failures")
    if empty_views:
        failures.append(f"{empty_views} reads gave an empty or no-collector view")
    if max_gap > gap_limit:
        failures.append(f"snapshot gap {max_gap:.2f}s over {gap_limit:.1f}s")
    if max_views_gap > gap_limit:
        failures.append(f"views.json gap {max_views_gap:.2f}s over {gap_limit:.1f}s")
    if phase == "stress" and not stale_seen:
        failures.append("hung source never read stale or down")
    return {
        "phase": phase, "cpu_percent": round(cpu_pct, 2), "rss_mb": round(rss_mb, 1),
        "max_snapshot_gap_s": round(max_gap, 2), "max_views_gap_s": round(max_views_gap, 2),
        "stale_seen": stale_seen, "reads": reads, "parse_failures": parse_failures,
        "cpu_budget": limit, "window_s": window_s, "loops": loops,
        "load_avg": [round(x, 1) for x in os.getloadavg()], "failures": failures,
    }


def report(result: dict) -> None:
    """Print one JSON line and append a markdown row to the CI step summary when set."""
    global _summary_header_done
    print(json.dumps(result, sort_keys=True), flush=True)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        if not _summary_header_done:
            fh.write("\n### FAFO collector overhead\n\n")
            fh.write("| phase | cpu % | budget % | rss MB | max snapshot gap s | stale seen "
                     "| reads | parse failures |\n|---|---|---|---|---|---|---|---|\n")
            _summary_header_done = True
        fh.write(
            f"| {result['phase']} | {result['cpu_percent']} | {result['cpu_budget']} "
            f"| {result['rss_mb']} | {result['max_snapshot_gap_s']} | {result['stale_seen']} "
            f"| {result['reads']} | {result['parse_failures']} |\n"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--phase", choices=("idle", "active", "stress", "all"), default="all")
    ap.add_argument("--window", type=float, default=20.0)
    ap.add_argument("--loops", type=int, default=4)
    args = ap.parse_args(argv)
    if not 1.0 <= args.window <= MAX_WINDOW_S or not 1 <= args.loops <= MAX_LOOPS:
        ap.error(f"window must be 1..{MAX_WINDOW_S:g} s and loops 1..{MAX_LOOPS}")
    sys.path.insert(0, str(SRC))
    phases = ("idle", "active", "stress") if args.phase == "all" else (args.phase,)
    bad = 0
    for ph in phases:
        res = run_phase(ph, args.window, args.loops if ph == "stress" else 0)
        report(res)
        bad += len(res["failures"])
    print(f"stress CPU budget is {STRESS_ALLOWANCE:g}x the idle budget "
          f"({STRESS_CPU_BUDGET:g} percent): contention allowance", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
