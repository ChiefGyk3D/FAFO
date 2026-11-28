"""Collector engine: one thread per source, hard timeouts, backoff, a snapshot every tick.

A source call runs in a daemon worker thread. On timeout the worker is abandoned, never joined,
so a hung call can delay nothing else: not another source, not the snapshot writer, not stop().
Redaction is the sources' job, not this module's.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

from .config import Config, SourceCfg
from .model import MAX_LLM_SERVERS, SNAPSHOT_VERSION, Snapshot, SourceHealth, Status
from .redact import mask_credentials, sanitize_text
from .render.export import write_views
from .render.view import build_views, policy_from_config
from .source import Ctx, Source, SourceResult
from .store import write_snapshot

BACKOFF_CAP_S = 60.0
ERROR_MAX_LEN = 200
_FIELD_MAX_LEN = 1000
_MAX_DEPTH = 8
_POLL_SLICE_S = 0.05
_MIN_TICK_S = 0.05
MEMO_MAX_ENTRIES = 8192
MEMO_MAX_KEY_LEN = 2000  # longer strings bypass the memo

_log = logging.getLogger("fafo.collector")


def _clean_error(text: str) -> str:
    """Make exception text safe for the snapshot: line breaks to spaces, then sanitize."""
    flat = str(text).replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return sanitize_text(flat, ERROR_MAX_LEN)


def backoff_delay(interval_s: float, failures: int) -> float:
    """Exponential delay after `failures` consecutive failures, capped at 60 s."""
    if failures <= 0:
        return interval_s
    grown = interval_s * (2 ** min(failures, 30))
    return max(interval_s, min(BACKOFF_CAP_S, grown))


_FIELD_NAMES: dict[type, tuple[str, ...]] = {}


def _init_field_names(cls: type) -> tuple[str, ...]:
    """Field names of a dataclass type, cached (dataclasses.fields() is costly per row)."""
    names = _FIELD_NAMES.get(cls)
    if names is None:
        names = tuple(f.name for f in dataclasses.fields(cls) if f.init)
        _FIELD_NAMES[cls] = names
    return names


class _Memo:
    """Bounded LRU of string scrub results, one per Collector (the alias map lives in cfg).

    No raw input is ever stored: a Proc.cmd arrives before redaction and can hold tokens and
    passwords, and the memo lives as long as the collector. The key is (kind, a keyed BLAKE2b
    digest of the input, 32 bytes, keyed with 32 random bytes from os.urandom per instance), and
    the value is the already scrubbed output. This replaces the earlier rule of keying by the
    exact string: with a 256-bit keyed cryptographic digest an accidental collision (which would
    return another string's scrub) has negligible probability, about 2**-256 per pair, and the
    random key means nobody can precompute a colliding input. Strings over MEMO_MAX_KEY_LEN
    bypass the memo and are never stored. Values are immutable str. Called from the per-source
    worker threads, so every access holds the lock.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._hash_key = os.urandom(32)
        self._data: OrderedDict[tuple[str, bytes], str] = OrderedDict()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def _digest(self, s: str) -> bytes:
        return hashlib.blake2b(
            s.encode("utf-8", "surrogatepass"), digest_size=32, key=self._hash_key
        ).digest()

    def _get(self, kind: str, s: str, compute: Callable[[str], str]) -> str:
        if len(s) > MEMO_MAX_KEY_LEN:
            return compute(s)
        key = (kind, self._digest(s))
        with self._lock:
            hit = self._data.get(key)
            if hit is not None:
                self._data.move_to_end(key)
                return hit
        value = compute(s)
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > MEMO_MAX_ENTRIES:
                self._data.popitem(last=False)
        return value

    def text(self, s: str) -> str:
        return self._get("text", s, lambda x: self._cfg.alias(sanitize_text(x, _FIELD_MAX_LEN)))

    def label(self, s: str) -> str:
        """Credential-mask a display label (task, branch, workflow...); the text scrub follows."""
        if not isinstance(s, str):
            return mask_credentials(s)
        return self._get("label", s, mask_credentials)

    def cmd(self, s: str) -> str:
        if not isinstance(s, str):
            return mask_credentials(s)  # returns "" for a non-str
        return self._get("cmd", s, mask_credentials)


def _scrub(obj: Any, cfg: Config, depth: int = 0, memo: _Memo | None = None) -> Any:
    """Sanitize every string (and dict key) in a row and apply host aliases. Fail closed."""
    if depth > _MAX_DEPTH:
        raise ValueError("row nesting too deep")
    if isinstance(obj, str):
        if memo is not None:
            return memo.text(obj)
        return cfg.alias(sanitize_text(obj, _FIELD_MAX_LEN))
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        names = _init_field_names(type(obj))
        changes = {n: _scrub(getattr(obj, n), cfg, depth + 1, memo) for n in names}
        return dataclasses.replace(obj, **changes)
    if isinstance(obj, dict):
        return {
            _scrub(str(k), cfg, depth + 1, memo): _scrub(v, cfg, depth + 1, memo)
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_scrub(v, cfg, depth + 1, memo) for v in obj]
    return obj


_SESSION_LABELS = ("task", "branch", "worktree", "name")
_RUNNER_LABELS = ("repo", "workflow", "job", "step")


def _scrub_result(res: SourceResult, cfg: Config, memo: _Memo | None = None) -> SourceResult:
    # Mask before the per-field length cap: a secret straddling the cap must not leak a prefix.
    # Proc.cmd and the free-text display fields of session and runner rows (task, branch, worktree,
    # workflow, job, step...) are masked: a source may hand over text that came from outside.
    mask = memo.cmd if memo is not None else mask_credentials
    label = memo.label if memo is not None else mask_credentials
    procs = [dataclasses.replace(p, cmd=mask(p.cmd)) for p in res.procs]
    sessions = [
        dataclasses.replace(
            x,
            **{f: label(getattr(x, f)) for f in _SESSION_LABELS},
        )
        for x in res.sessions
    ]
    runners = [
        dataclasses.replace(r, **{f: label(getattr(r, f)) for f in _RUNNER_LABELS})
        for r in res.runners
    ]
    return _scrub(
        dataclasses.replace(res, procs=procs, sessions=sessions, runners=runners), cfg, 0, memo
    )


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
        views_path: Path | None = None,
        write_views_file: bool = True,
    ) -> None:
        self._cfg = cfg
        self._store_path = Path(store_path)
        self._views_path = (
            Path(views_path) if views_path is not None else self._store_path.with_name("views.json")
        )
        self._write_views_file = write_views_file
        self.views_write_failures = 0
        self._clock = clock
        self._memo = _Memo(cfg)
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
            return None, "timeout: previous call still hung", True
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
        if res.status is not None and res.status not in get_args(Status):
            return None, "invalid status from source", False
        try:
            return _scrub_result(res, self._cfg, self._memo), "", False
        except Exception as exc:  # noqa: BLE001 - malformed rows count as a failed call
            return None, f"unusable rows: {type(exc).__name__}", False

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
            snap.llms.extend(res.llms or ([res.llm] if res.llm is not None else []))
            snap.host.update(res.host)
            snap.quota.update(res.quota)
        del snap.llms[MAX_LLM_SERVERS:]
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
            snap: Snapshot | None = None
            try:
                snap = self.snapshot()
                write_snapshot(self._store_path, snap)
            except Exception:  # noqa: BLE001 - a failed write must not end the collector
                snap = None
                _log.exception("snapshot write failed")
            if snap is not None and self._write_views_file:
                self._write_views(snap)
            self._stop.wait(self._tick_s)

    def _write_views(self, snap: Snapshot) -> None:
        """Write views.json from the snapshot just written. Never raises; logs no values."""
        try:
            now = self._clock()
            views = build_views(snap, now, helpers=policy_from_config(self._cfg))
            write_views(self._views_path, views, snapshot=snap, generated=now)
        except Exception as exc:  # noqa: BLE001 - views are optional, the snapshot is not
            self.views_write_failures += 1
            n = self.views_write_failures
            if n == 1 or n % 100 == 0:  # first and every 100th: visible, not one line per tick
                _log.warning(
                    "views.json write failed (%d so far): %s", n, type(exc).__name__
                )

    def stop(self) -> None:
        """Ask everything to end. Never joins a worker, so a hung source cannot block it."""
        self._stop.set()
        for t in self._threads:
            t.join(timeout=0.5)  # loop threads poll the stop flag every 50 ms
