"""Process object cache so psutil CPU readings are real, not always 0.0.

psutil's `cpu_percent(None)` measures the time since the previous call on the SAME Process
object. A Process built fresh each collector cycle therefore always reads 0.0. This cache keeps
the objects across cycles, keyed by (pid, create_time) so a recycled pid never reuses an old
object.

The first cycle after a process is first seen reports 0.0 (the object is primed and the reading
discarded). Later cycles report the measured value.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from typing import Any

import psutil

MAX_ENTRIES = 4096

Key = tuple[int, float]


class ProcCache:
    """Cross-cycle store of primed psutil Process objects. One instance per source."""

    def __init__(self, psutil_mod: Any = psutil, max_entries: int = MAX_ENTRIES) -> None:
        self._ps = psutil_mod
        self._max = max(1, min(int(max_entries), MAX_ENTRIES))
        self._items: OrderedDict[Key, Any] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def _lookup(self, pid: int, create_time: float, proc: Any) -> tuple[Any, bool] | None:
        """(object, fresh) for the key; None if the object cannot be created or primed.

        Caller holds the lock.
        """
        key = (int(pid), float(create_time))
        obj = self._items.get(key)
        if obj is not None:
            return obj, False
        try:
            obj = proc if proc is not None else self._ps.Process(key[0])
            obj.cpu_percent(None)  # priming call; the value is discarded
        except Exception:  # noqa: BLE001 - vanished, denied or odd: no entry, caller reports 0.0
            return None
        self._items[key] = obj
        while len(self._items) > self._max:
            self._items.popitem(last=False)  # oldest first
        return obj, True

    def get(self, pid: int, create_time: float, proc: Any = None) -> Any | None:
        """The cached Process for (pid, create_time); created and primed on first sight.

        `proc` is an already built Process to adopt instead of building one. None if the
        process cannot be created or primed.
        """
        with self._lock:
            found = self._lookup(pid, create_time, proc)
        return None if found is None else found[0]

    def cpu(self, pid: int, create_time: float, proc: Any = None) -> float:
        """CPU percent since the previous call. 0.0 on first sight or any failure.

        A process whose object raises while being read is dropped from the cache.
        """
        key = (int(pid), float(create_time))
        with self._lock:
            found = self._lookup(pid, create_time, proc)
            if found is None:
                return 0.0
            obj, fresh = found
            if fresh:
                return 0.0
            try:
                value = float(obj.cpu_percent(None))
            except Exception:  # noqa: BLE001 - NoSuchProcess, AccessDenied or anything odd
                self._items.pop(key, None)
                return 0.0
        return value if math.isfinite(value) and value > 0.0 else 0.0

    def end_cycle(self, seen: set[Key]) -> None:
        """Drop every entry not in `seen`, so exited processes do not accumulate."""
        with self._lock:
            for key in [k for k in self._items if k not in seen]:
                del self._items[key]
