"""Overhead and load proof: the collector stays cheap and the display never blanks."""

import os

import measure_overhead as mo
import pytest

WINDOW_S = 10.0
FULL_WINDOW_S = 30.0
DEFAULT_LOOPS = 4
FULL_LOOPS = 8


def _check(res: dict) -> None:
    mo.report(res)
    assert not res["failures"], res["failures"]


@pytest.mark.perf
def test_idle_within_budget():
    res = mo.run_phase("idle", WINDOW_S)
    _check(res)
    assert res["reads"] > 0  # something was actually measured


@pytest.mark.perf
def test_active_within_budget():
    res = mo.run_phase("active", WINDOW_S)
    _check(res)
    assert res["reads"] > 0


@pytest.mark.perf
def test_active_cap_within_budget(perf_full):
    """Worst case the display caps allow (7 x 256 changing rows); the full variant only."""
    if not perf_full:
        pytest.skip("full variant only: pytest tests/perf --perf-full or FAFO_PERF_FULL=1")
    res = mo.run_phase("active-cap", FULL_WINDOW_S)
    _check(res)
    assert res["reads"] > 0


@pytest.mark.perf
def test_stress_display_never_blanks(perf_full):
    window, loops = (FULL_WINDOW_S, FULL_LOOPS) if perf_full else (WINDOW_S, DEFAULT_LOOPS)
    loops = min(loops, os.cpu_count() or 1, mo.MAX_LOOPS)
    res = mo.run_phase("stress", window, loops)
    # Contention allowance: the CPU budget here is 2x the idle budget (see measure_overhead.py).
    print(f"stress CPU budget {res['cpu_budget']} percent = {mo.STRESS_ALLOWANCE:g}x idle")
    _check(res)
    assert res["stale_seen"]
    assert res["reads"] > 0
    assert res["parse_failures"] == 0
    assert res["max_snapshot_gap_s"] <= mo.MAX_GAP_FACTOR * mo.TICK_S, (
        f"snapshot gap {res['max_snapshot_gap_s']}s"
    )


@pytest.mark.perf
def test_views_doc_check_catches_a_valid_but_empty_document():
    """No collector: a fake document that parses, says ok, and lists no sessions must fail."""
    empty_ok = {"schema": 2, "generated": 1.0, "status": "ok", "sessions": [],
                "session_index_count": 0}
    assert mo.check_views_doc(empty_ok, sessions_existed=True)
    assert mo.check_views_doc({**empty_ok, "status": "no-collector", "session_index_count": 3}, True)
    assert mo.check_views_doc({"status": "ok", "sessions": [{"pid": 1}]}, True)  # no schema
    good = {"schema": 2, "generated": 1.0, "status": "stale", "sessions": [{"pid": 1}]}
    assert mo.check_views_doc(good, True) == []
    assert mo.check_views_doc({**empty_ok, "session_index_count": 2}, True) == []
    assert mo.check_views_doc(empty_ok, sessions_existed=False) == []  # nothing to blank yet
    assert mo.check_views_doc(["x"], True)
