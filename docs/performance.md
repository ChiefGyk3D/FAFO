# Performance and load behavior

FAFO must never blank the display and must never add meaningful load. This page states what is
measured, the budgets, how to run the measurement, and the numbers seen so far.

## What is measured

A real `Collector` runs as its own process (`tests/perf/stubs.py`) with stub sources, so the CPU
and memory read through psutil belong to the collector alone. CPU is the collector's own
`cpu_times()` delta over the window divided by wall time (percent of one core). Memory is the
resident set size at the end of the window. The tick is 2 s, the default (the smallest default
source interval).

| Phase | Setup | Budget |
| --- | --- | --- |
| idle | 7 sessions with 20 children each, static rows, one cheap host source | CPU under 2 percent, RSS under 100 MB |
| active | 7 sessions with 256 children each, every row changing every call | CPU under 5 percent, RSS under 100 MB |
| stress | idle-size rows, busy-loop processes (one per core, at most 8), and a source that answers once and then blocks forever | CPU under 4 percent (2x the idle budget, a contention allowance), RSS under 100 MB, plus the checks below |

Stress checks, on a read of the snapshot and `views.json` every 0.25 s for the whole window:

- the snapshot `taken` time and the `generated` time in `views.json` advance with no gap longer
  than 3 ticks (6 s);
- every read parses (never a partial file);
- the hung source reads `stale` or `down` and keeps its rows once it has been seen stale;
- `build_views` never returns an empty tree or a `no-collector` status while sessions existed.

## Run it

- Default (CI friendly, under 60 s for the file on an idle machine): `pytest tests/perf`. The idle and active phases
  use a 10 s window. The stress phase uses a 10 s window and 4 loops.
- Full stress variant (30 s window, 8 loops): `pytest tests/perf -m perf --perf-full`, or set
  `FAFO_PERF_FULL=1`. The `--perf-full` option is registered by `tests/perf/conftest.py`, so it
  is known only when `tests/perf` is part of the invocation; the environment variable works
  anywhere.
- Script with a longer window (up to 120 s, loops up to 8): `scripts/measure-overhead.sh
  --phase all --window 60 --loops 8`. It prints one JSON line per phase and exits 1 when a budget
  or continuity check fails. Set `FAFO_PYTHON` to an interpreter that has psutil.
- When `GITHUB_STEP_SUMMARY` is set, a markdown table of the numbers is appended to that file.

Each JSON line has: `phase`, `cpu_percent`, `rss_mb`, `max_snapshot_gap_s`, `max_views_gap_s`,
`stale_seen`, `reads`, `parse_failures`, the budget, the window, the loops and the load average.

## Numbers seen on the development machine

The machine has 8 cores and was heavily loaded by other work (load average 47 to 69 during these
runs), so CPU times are inflated compared with an idle host.

| Phase | CPU percent of one core | RSS | Max snapshot gap | Notes |
| --- | --- | --- | --- | --- |
| idle | 0.4 to 0.6 | 17 to 18 MB | 2.2 s | within budget |
| active (after the scrub memo) | 3.5 to 4.5 | 21 MB | 2.6 s | within budget, thin margin on a loaded host |
| active (memo disabled) | 7.5 | 21 MB | 2.2 s | over budget |
| stress, 10 s, 4 loops | 0.3 to 0.6 | 17 MB | 2.3 s | hung source stale, no parse failures |
| stress, 30 s, 8 loops | 0.9 (earlier run, before the memo) | 18 MB | 2.2 s | hung source stale, no parse failures |

The active phase is the worst case the display caps allow (1792 changing process rows). Redacting
and sanitizing those rows was the main cost (about 0.14 s of CPU per call). The collector now
memoizes the string scrub per instance (bounded LRU of 8192 entries keyed by kind and the exact
string, strings over 2000 characters bypass it), which cut the active figure from about 7.5 to
about 4. The remaining cost is mostly the sanitizing in `build_views`, the dataclass walk and the
JSON encoding of the snapshot.

## Revert proof

With the collector changed to wait for an abandoned (hung) worker before every snapshot write, the
stress test fails: snapshot gap 10.24 s over 6.0 s, `views.json` gap 10.24 s over 6.0 s, and the
hung source never read stale. The test window is bounded, so a regression fails instead of
hanging the suite.
