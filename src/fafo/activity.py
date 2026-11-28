"""Active or idle for a worker, from how its cumulative CPU time moved over a few samples.

Pure: no I/O, and the clock is injected. One sample per collect cycle per process goes in; the
tracker keeps the last few for each (pid, create_time) and classifies from the oldest and newest
of them. A pid reused by a new process has a different create_time, so it starts a new series.

Never guesses: with fewer than two usable samples, or when a value cannot be trusted, the answer
is `unknown`.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import Any, Literal, get_args

Activity = Literal["active", "idle", "unknown"]
ACTIVITIES: tuple[str, ...] = get_args(Activity)

WINDOW = 4  # samples kept per process
MAX_ENTRIES = 2048  # processes tracked at once
EXPIRY_S = 60.0  # an entry not seen for this long is dropped
# The old ai-status tool used 1.0 (percent of one core, sampled over 250 ms). CPU time comes in
# ticks of 10 ms, so a window shorter than MIN_SPAN_S is too coarse to judge and stays unknown.
DEFAULT_ACTIVE_PCT = 1.0
MIN_ACTIVE_PCT = 0.5
MAX_ACTIVE_PCT = 100.0
MIN_SPAN_S = 0.5
MAX_CPU_TIME_S = 1e9  # about 31 years of CPU time; anything above is not a real reading
MAX_EPOCH_S = 4e9  # epoch seconds (year 2096): the bound for process start times and the clock

Key = tuple[int, float]


def _in_range(value: object, low: float, high: float) -> bool:
    """True for an int or a float (never bool) from low to high inclusive. An int is compared as
    an int, so a huge one is never converted to float and cannot overflow; NaN and inf fail."""
    if type(value) is int:
        return low <= value <= high
    if type(value) is float:
        return math.isfinite(value) and low <= value <= high
    return False


def valid_pct(value: object) -> bool:
    """True for a real number (not bool) from MIN_ACTIVE_PCT to MAX_ACTIVE_PCT."""
    return _in_range(value, MIN_ACTIVE_PCT, MAX_ACTIVE_PCT)


def clean_activity(value: object) -> str:
    """The value if it is one of the three words, else `unknown`."""
    return value if type(value) is str and value in ACTIVITIES else "unknown"


def _good_cpu_time(value: object) -> bool:
    return _in_range(value, 0.0, MAX_CPU_TIME_S)


def _good_start(value: object) -> bool:
    """A process start time is epoch seconds (psutil create_time), not CPU seconds."""
    return _in_range(value, 0.0, MAX_EPOCH_S)


class ActivityTracker:
    def __init__(
        self,
        active_pct: float = DEFAULT_ACTIVE_PCT,
        *,
        clock: Callable[[], float] = time.monotonic,
        window: int = WINDOW,
        max_entries: int = MAX_ENTRIES,
        expiry_s: float = EXPIRY_S,
    ) -> None:
        if not valid_pct(active_pct):
            raise ValueError("active_pct out of range")
        self._pct = float(active_pct)
        self._clock = clock
        self._window = max(2, min(int(window), 64))
        self._max = max(1, min(int(max_entries), MAX_ENTRIES))
        self._expiry = float(expiry_s)
        # key -> (last seen, [(time, cpu_time), ...]); least recently seen first
        self._items: OrderedDict[Key, tuple[float, list[tuple[float, float]]]] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def _purge(self, now: float) -> None:
        while self._items:
            key = next(iter(self._items))
            if now - self._items[key][0] > self._expiry:
                del self._items[key]
            else:
                break

    def observe(self, pid: object, create_time: object, cpu_time: object) -> Activity:
        """Record one reading of cumulative CPU seconds and return the classification."""
        if type(pid) is not int or not _good_start(create_time):
            return "unknown"
        try:
            raw_now = self._clock()
        except Exception:  # noqa: BLE001 - a broken injected clock must not break a cycle
            return "unknown"
        if not _in_range(raw_now, 0.0, MAX_EPOCH_S):
            return "unknown"
        now = float(raw_now)
        key: Key = (pid, float(create_time))  # type: ignore[arg-type]
        with self._lock:
            self._purge(now)
            if not _good_cpu_time(cpu_time):
                self._items.pop(key, None)  # an untrusted reading ends the series
                return "unknown"
            cpu = float(cpu_time)  # type: ignore[arg-type]
            entry = self._items.get(key)
            samples = entry[1] if entry is not None else []
            if samples and (now < samples[-1][0] or cpu < samples[-1][1]):
                samples = []  # the clock or the counter went backwards: start again
            if samples and now == samples[-1][0]:
                samples[-1] = (now, cpu)
            else:
                samples.append((now, cpu))
            del samples[: -self._window]
            self._items[key] = (now, samples)
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)
            return self._classify(samples)

    def _classify(self, samples: list[tuple[float, float]]) -> Activity:
        if len(samples) < 2:
            return "unknown"
        (t0, c0), (t1, c1) = samples[0], samples[-1]
        span = t1 - t0
        if not span >= MIN_SPAN_S:
            return "unknown"
        return "active" if (c1 - c0) / span * 100.0 >= self._pct else "idle"


def observe_activity(tracker: ActivityTracker | None, pid: int, entry: Any) -> Activity:
    """Read user plus system CPU seconds from entry.handle and record them. Any failure to read
    (a vanished process, a handle without cpu_times, odd values) is `unknown`, never an error."""
    if tracker is None:
        return "unknown"
    try:
        t = entry.handle.cpu_times()
        total = t.user + t.system
        return tracker.observe(pid, entry.started, total)
    except Exception:  # noqa: BLE001 - psutil errors, missing method, odd types, overflow
        return "unknown"


def counts(states: Iterable[object], fallback_active: Iterable[bool]) -> tuple[int, int]:
    """(active, total) for workers: the activity word when known, else the fallback flag (the
    older CPU rule) for that worker. Both iterables are in the same order."""
    active = total = 0
    for state, fb in zip(states, fallback_active):
        total += 1
        word = clean_activity(state)
        if word == "active" or (word == "unknown" and fb):
            active += 1
    return active, total
