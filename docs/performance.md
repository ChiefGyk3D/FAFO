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
| active | 7 sessions with 20 children each, every row changing every call (the typical size) | CPU under 5 percent, RSS under 100 MB |
| active-cap | 7 sessions with 256 children each, every row changing (the display cap, the worst case); full variant only | CPU under 5 percent, RSS under 100 MB |
| stress | idle-size rows, busy-loop processes (one per core, at most 8), and a source that answers once and then blocks forever | CPU under 4 percent (2x the idle budget, a contention allowance), RSS under 100 MB, plus the checks below |

Stress checks, on a read of the snapshot and `views.json` every 0.25 s for the whole window:

- the snapshot `taken` time and the `generated` time in `views.json` advance with no gap longer
  than 3 ticks (6 s);
- every read parses (never a partial file), and the `views.json` document itself is checked:
  schema present, status not `no-collector`, `sessions` (or a positive `session_index_count`)
  non-empty while sessions exist;
- the hung source reads `stale` or `down` and keeps its rows once it has been seen stale;
- `build_views` never returns an empty tree or a `no-collector` status while sessions existed.

## Run it

- Default run (CI friendly, under 60 s on an idle machine): `pytest tests/perf`. The idle and
  active phases use a 10 s window, the stress phase a 10 s window and 4 loops. The active-cap
  test is skipped.
- Full run: `pytest tests/perf -m perf --perf-full`, or `FAFO_PERF_FULL=1 pytest tests/perf`.
  Every test in the directory is marked `perf`, so `-m perf` selects them all (idle, active,
  active-cap, stress, and the views.json check test); the full run adds the active-cap phase
  (7x256, 30 s) and makes the stress phase use a 30 s window and 8 loops. `--perf-full` is
  registered by `tests/perf/conftest.py`, so it is known only when `tests/perf` is part of the
  invocation; the environment variable works anywhere.
- Script with a longer window (up to 120 s, loops up to 8): `scripts/measure-overhead.sh
  --phase all --window 60 --loops 8`. It prints one JSON line per phase and exits 1 when a budget
  or continuity check fails. Set `FAFO_PYTHON` to an interpreter that has psutil.
- When `GITHUB_STEP_SUMMARY` is set, a markdown table of the numbers is appended to that file.

Each JSON line has: `phase`, `cpu_percent`, `rss_mb`, `max_snapshot_gap_s`, `max_views_gap_s`,
`stale_seen`, `reads`, `parse_failures`, the budget, the window, the loops and the load average.

## Numbers seen on the development machine

The machine has 8 cores and was heavily loaded by other work (load average 47 to 69 during these
runs), so CPU times are inflated compared with an idle host.

| Phase | Profile | CPU percent of one core | RSS | Max snapshot gap |
| --- | --- | --- | --- | --- |
| idle | 7x20, static | 0.4 to 0.6 | 17.6 MB | 2.2 s |
| active | 7x20, changing | 0.5 | 17.6 MB | 2.3 s |
| active, memo disabled (measurement check) | 7x20, changing | 0.8 | 17.6 MB | n/a |
| active-cap (full variant, 30 s) | 7x256, changing | 4.5 | 21 MB | 2.9 s |
| active-cap, before the scrub memo | 7x256, changing | 7.0 to 7.5 | 21 MB | 2.2 s |
| stress, 10 s, 4 loops | 7x20, static, hung source | 0.3 to 0.6 | 17.6 MB | 2.2 s |
| stress, 30 s, 8 loops (full) | 7x20, static, hung source | 0.6 | 18 MB | 2.6 s |

Every JSON line carries a `profile` field naming the profile the number comes from. The typical
profile has a wide margin (0.5 of 5.0 percent). The display-cap profile (1792 changing process
rows) is the worst case the caps allow; it runs only in the full variant, with the same 5 percent
budget, and its 4.5 percent leaves a thin margin on a loaded host.

Redacting and sanitizing rows was the main cost of the cap profile (about 0.14 s of CPU per
call). The collector memoizes the string scrub per instance (bounded LRU of 8192 entries keyed by
kind and the exact string; strings over 2000 characters bypass it), which cut the cap figure from
about 7.5 to about 4.5. The remaining cost is mostly the sanitizing in `build_views`, the
dataclass walk and the JSON encoding of the snapshot.

## Revert proof

With the collector changed to wait for an abandoned (hung) worker before every snapshot write, the
stress test fails: snapshot gap 10.24 s over 6.0 s, `views.json` gap 10.24 s over 6.0 s, and the
hung source never read stale. The test window is bounded, so a regression fails instead of
hanging the suite.
