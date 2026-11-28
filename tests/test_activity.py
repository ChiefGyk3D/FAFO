import math

import pytest

from fafo.activity import (
    EXPIRY_S,
    MAX_ENTRIES,
    ActivityTracker,
    clean_activity,
    counts,
    valid_pct,
)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def mk(**kw):
    c = Clock()
    return ActivityTracker(clock=c, **kw), c


def test_rising_cpu_is_active_after_two_samples():
    t, c = mk()
    assert t.observe(5, 10.0, 1.0) == "unknown"
    c.t += 2.0
    assert t.observe(5, 10.0, 1.5) == "active"  # 25 percent of a core


def test_flat_cpu_is_idle():
    t, c = mk()
    t.observe(5, 10.0, 3.0)
    c.t += 2.0
    assert t.observe(5, 10.0, 3.0) == "idle"
    c.t += 2.0
    assert t.observe(5, 10.0, 3.0) == "idle"


def test_threshold_boundary_and_option():
    t, c = mk(active_pct=50.0)
    t.observe(1, 1.0, 0.0)
    c.t += 2.0
    assert t.observe(1, 1.0, 0.9) == "idle"  # 45 percent
    t2, c2 = mk(active_pct=50.0)
    t2.observe(1, 1.0, 0.0)
    c2.t += 2.0
    assert t2.observe(1, 1.0, 1.0) == "active"  # exactly 50


def test_one_sample_is_unknown_and_short_span_is_unknown():
    t, c = mk()
    assert t.observe(5, 10.0, 1.0) == "unknown"
    c.t += 0.1
    assert t.observe(5, 10.0, 9.0) == "unknown"  # too short a window to judge


def test_pid_reuse_starts_new_series():
    t, c = mk()
    t.observe(5, 10.0, 100.0)
    c.t += 2.0
    assert t.observe(5, 10.0, 100.0) == "idle"
    c.t += 2.0
    # same pid, different create_time: a new process, so one sample only
    assert t.observe(5, 99.0, 0.1) == "unknown"
    assert len(t) == 2


def test_window_keeps_last_four():
    t, c = mk()
    t.observe(5, 1.0, 0.0)
    for _ in range(3):
        c.t += 2.0
        t.observe(5, 1.0, 0.0)  # idle
    for _ in range(4):  # busy now; the idle samples fall out of the window
        c.t += 2.0
        last = t.observe(5, 1.0, t._items[(5, 1.0)][1][-1][1] + 2.0)
    assert last == "active"


def test_bounded_and_expiry():
    t, c = mk(max_entries=3)
    for pid in range(10):
        t.observe(pid, 1.0, 0.0)
        c.t += 0.001
    assert len(t) == 3
    assert (9, 1.0) in t._items and (0, 1.0) not in t._items
    c.t += EXPIRY_S + 1
    t.observe(100, 1.0, 0.0)
    assert len(t) == 1
    assert MAX_ENTRIES == 2048
    assert ActivityTracker(max_entries=10**9)._max == 2048


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), 1e30, "3", None, True, [1]])
def test_hostile_cpu_values_are_unknown_and_never_raise(bad):
    t, c = mk()
    t.observe(5, 1.0, 1.0)
    c.t += 2.0
    assert t.observe(5, 1.0, bad) == "unknown"
    c.t += 2.0
    assert t.observe(5, 1.0, 2.0) == "unknown"  # the series restarted


def test_decreasing_cpu_resets_series():
    t, c = mk()
    t.observe(5, 1.0, 50.0)
    c.t += 2.0
    assert t.observe(5, 1.0, 10.0) == "unknown"
    c.t += 2.0
    assert t.observe(5, 1.0, 12.0) == "active"


@pytest.mark.parametrize("pid,ct", [("x", 1.0), (True, 1.0), (1, float("nan")), (1, None), (1, -5.0)])
def test_hostile_identity_is_unknown(pid, ct):
    t, _ = mk()
    assert t.observe(pid, ct, 1.0) == "unknown"
    assert len(t) == 0


def test_clock_going_backwards_or_broken_never_raises():
    t, c = mk()
    t.observe(5, 1.0, 1.0)
    c.t -= 10.0
    assert t.observe(5, 1.0, 9.0) == "unknown"

    def boom():
        raise RuntimeError

    assert ActivityTracker(clock=boom).observe(5, 1.0, 1.0) == "unknown"
    assert ActivityTracker(clock=lambda: math.nan).observe(5, 1.0, 1.0) == "unknown"


def test_valid_pct_and_constructor():
    for ok in (0.5, 1, 1.0, 100):
        assert valid_pct(ok)
    for bad in (0.49, 100.1, float("nan"), True, "1", None, -1):
        assert not valid_pct(bad)
    with pytest.raises(ValueError):
        ActivityTracker(0.1)


def test_clean_activity_and_counts():
    assert clean_activity("active") == "active"
    for bad in ("ACTIVE", None, 3, "x"):
        assert clean_activity(bad) == "unknown"
    # known words win over the fallback; unknown uses the fallback flag
    assert counts(["active", "idle", "unknown", "unknown"], [False, True, True, False]) == (2, 4)


REAL_START = 1_795_000_000.0  # a psutil create_time: epoch seconds in late 2026
SMALL_START = 10.0  # a start the current code accepts, so these tests isolate their own check
HOSTILE = [10**400, -(10**400), True, False, "1", None, float("nan"), float("inf"), float("-inf")]


def test_realistic_epoch_start_is_classified():
    t, c = mk()
    assert t.observe(5, REAL_START, 1.0) == "unknown"
    c.t += 2.0
    assert t.observe(5, REAL_START, 1.5) == "active"
    for _ in range(4):  # the window drains the busy period
        c.t += 2.0
        last = t.observe(5, REAL_START, 1.5)
    assert last == "idle"


@pytest.mark.parametrize("start", [0.0, 4_000_000_000.0])
def test_start_time_bounds_are_inclusive(start):
    t, c = mk()
    t.observe(5, start, 1.0)
    c.t += 2.0
    assert t.observe(5, start, 1.5) == "active"


@pytest.mark.parametrize(
    "start", [-1.0, -(10**400), 4_000_000_001.0, 1e12, 10**400, float("nan"), float("inf"),
              True, "1795000000", None],
)
def test_start_time_out_of_range_is_unknown(start):
    t, c = mk()
    assert t.observe(5, start, 1.0) == "unknown"
    c.t += 2.0
    assert t.observe(5, start, 1.5) == "unknown"
    assert len(t) == 0


@pytest.mark.parametrize("bad", HOSTILE)
def test_observe_cpu_time_hostile_never_raises(bad):
    t, c = mk()
    t.observe(5, SMALL_START, 1.0)
    c.t += 2.0
    assert t.observe(5, SMALL_START, bad) == "unknown"


@pytest.mark.parametrize("bad", HOSTILE)
def test_observe_start_time_hostile_never_raises(bad):
    t, _ = mk()
    assert t.observe(5, bad, 1.0) == "unknown"
    assert len(t) == 0


@pytest.mark.parametrize("bad", HOSTILE)
def test_observe_clock_hostile_never_raises(bad):
    t, c = mk()
    t.observe(5, SMALL_START, 1.0)  # clock at 1000
    c.t = bad
    assert t.observe(5, SMALL_START, 2.0) == "unknown"


def test_bool_clock_is_not_read_as_one_second():
    t, c = mk()
    c.t = 0.5
    t.observe(5, SMALL_START, 1.0)
    c.t = True  # if read as 1.0 this would be a 200 percent rise over a 0.5 s span
    assert t.observe(5, SMALL_START, 2.0) == "unknown"


@pytest.mark.parametrize("bad", [10**400, -(10**400), True, False, float("nan"), float("inf"),
                                 float("-inf"), 0, 0.49, 100.1, "1", None])
def test_valid_pct_rejects_huge_ints_and_hostile_values(bad):
    assert valid_pct(bad) is False


def test_valid_pct_accepts_the_range_ends():
    assert valid_pct(0.5) is True
    assert valid_pct(100) is True
