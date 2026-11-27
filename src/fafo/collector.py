"""Collector engine: one thread per source, hard timeouts, backoff, a snapshot every tick.

A source call runs in a daemon worker thread. On timeout the worker is abandoned, never joined,
so a hung call can delay nothing else: not another source, not the snapshot writer, not stop().
Redaction is the sources' job, not this module's.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, SourceCfg
from .model import SNAPSHOT_VERSION, Snapshot, SourceHealth, Status
from .source import Ctx, Source, SourceResult
from .store import write_snapshot

BACKOFF_CAP_S = 60.0
ERROR_MAX_LEN = 200
_POLL_SLICE_S = 0.05
_MIN_TICK_S = 0.05

_log = logging.getLogger("fafo.collector")


def _clean_error(text: str, max_len: int = ERROR_MAX_LEN) -> str:
    """Strip control characters and cap the length.

    TODO(FAFO#4): switch to fafo.redact.sanitize_text once the redactor is merged.
    """
    out = []
    for ch in str(text):
        if ch in "\n\r\t":
            out.append(" ")
        elif ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F:
            continue
        else:
            out.append(ch)
    return "".join(out)[:max_len]


def backoff_delay(interval_s: float, failures: int) -> float:
    """Exponential delay after `failures` consecutive failures, capped at 60 s."""
    if failures <= 0:
        return interval_s
    grown = interval_s * (2 ** min(failures, 30))
    return max(interval_s, min(BACKOFF_CAP_S, grown))


@dataclass
class _State:
    source: Source
    cfg: SourceCfg
    ctx: Ctx
    lock: threading.Lock = field(default_factory=threading.Lock)
    status: Status = "stale"
    last_ok: float = 0.0
    error: str = ""
    failures: int = 0
    delay: float = 0.0
    result: SourceResult | None = None  # last good rows, kept unchanged while stale
    worker: threading.Thread | None = None


class Collector:
    def __init__(
        self,
        cfg: Config,
        sources: Sequence[Source],
        store_path: Path,
        *,
        clock: Callable[[], float] = time.time,
        tick_s: float | None = None,
    ) -> None:
        self._cfg = cfg
        self._store_path = Path(store_path)
        self._clock = clock
        self._stop = threading.Event()
        self._states: list[_State] = []
        for src in sources:
            scfg = cfg.sources.get(src.name, SourceCfg())
            if not scfg.enabled:
                continue
            ctx = Ctx(cfg=scfg, clock=clock, log=logging.getLogger(f"fafo.source.{src.name}"))
            self._states.append(_State(source=src, cfg=scfg, ctx=ctx, delay=scfg.interval_s))
        if tick_s is None:
            tick_s = min((s.cfg.interval_s for s in self._states), default=1.0)
        self._tick_s = max(_MIN_TICK_S, tick_s)
        self._threads: list[threading.Thread] = []

    # -- one source -------------------------------------------------------------------------

    def _call(self, st: _State) -> tuple[SourceResult | None, str, bool]:
        """Run collect() in a daemon worker. Returns (result, error, timed_out)."""
        if st.worker is not None and st.worker.is_alive():
            return None, "previous call still hung", True
        box: dict = {}
        done = threading.Event()

        def target() -> None:
            try:
                box["r"] = st.source.collect(st.ctx)
            except BaseException as exc:  # noqa: BLE001 - a source must never kill the engine
                box["e"] = exc
            finally:
                done.set()

        worker = threading.Thread(target=target, daemon=True, name=f"fafo-call-{st.source.name}")
        worker.start()
        deadline = time.monotonic() + st.cfg.timeout_s
        while not done.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                st.worker = worker  # abandoned: remembered only to avoid piling up threads
                return None, f"timeout after {st.cfg.timeout_s:g}s", True
            if self._stop.is_set():
                return None, "stopped", True
            done.wait(min(_POLL_SLICE_S, remaining))
        if "e" in box:
            exc = box["e"]
            return None, f"{type(exc).__name__}: {exc}", False
        res = box["r"]
        if not isinstance(res, SourceResult):
            return None, f"bad result type {type(res).__name__}", False
        return res, "", False

    def _poll(self, st: _State) -> None:
        res, err, timed_out = self._call(st)
        with st.lock:
            if res is not None:
                st.result = res
                st.failures = 0
                st.error = ""
                st.status = res.status or "ok"
                st.last_ok = self._clock()
                st.delay = st.cfg.interval_s
            else:
                st.failures += 1
                st.error = _clean_error(err)
                st.status = "stale" if timed_out else "down"
                st.delay = backoff_delay(st.cfg.interval_s, st.failures)

    def _loop(self, st: _State) -> None:
        while not self._stop.is_set():
            try:
                self._poll(st)
            except Exception:  # noqa: BLE001 - the loop itself must survive anything
                _log.exception("poll loop error for %s", st.source.name)
            self._stop.wait(st.delay)

    # -- snapshot ---------------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        snap = Snapshot(version=SNAPSHOT_VERSION, taken=self._clock(), host={})
        for st in self._states:
            with st.lock:
                res = st.result
                snap.health.append(
                    SourceHealth(
                        name=st.source.name, status=st.status, last_ok=st.last_ok, error=st.error
                    )
                )
            if res is None:
                continue
            snap.sessions.extend(res.sessions)
            snap.procs.extend(res.procs)
            snap.runners.extend(res.runners)
            if snap.llm is None and res.llm is not None:
                snap.llm = res.llm
            snap.host.update(res.host)
            snap.quota.update(res.quota)
        return snap

    def run(self) -> None:
        """Start the source threads and write a snapshot every tick until stop()."""
        for st in self._states:
            t = threading.Thread(
                target=self._loop, args=(st,), daemon=True, name=f"fafo-src-{st.source.name}"
            )
            self._threads.append(t)
            t.start()
        while not self._stop.is_set():
            try:
                write_snapshot(self._store_path, self.snapshot())
            except Exception:  # noqa: BLE001 - a failed write must not end the collector
                _log.exception("snapshot write failed")
            self._stop.wait(self._tick_s)

    def stop(self) -> None:
        """Ask everything to end. Never joins a worker, so a hung source cannot block it."""
        self._stop.set()
        for t in self._threads:
            t.join(timeout=0.5)  # loop threads poll the stop flag every 50 ms
