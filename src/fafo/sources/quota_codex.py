"""Codex quota source: the 5-hour and weekly rate-limit windows from the newest rollout file.

Codex records its rate limits inside its session rollout files, which are the owner's private
conversations. This module therefore reads as little as it can and keeps only numbers:

* The newest rollout file is chosen from directory entry names and `lstat` data alone (no file
  is opened to compare). Directories are walked through descriptors with no-follow; a symlink,
  a directory named like a rollout and any non-regular file is never a candidate.
* Exactly ONE file is opened (descriptor, no-follow, non-blocking so a FIFO cannot hang it),
  checked with `fstat` (regular file, owned by the user) and read from its last 64 KB only.
* Lines are parsed from the end backwards. A line is parsed only when it contains the literal
  `"rate_limits"` and is under the line cap and the nesting cap (checked by an iterative scan
  before any JSON parser sees it). Only the timestamp and, per window, `window_minutes`,
  `used_percent` and `resets_at` are taken; every other value is dropped at once.
* Nothing from the file is logged or stored except those numbers. Log lines carry fixed reason
  words only.

Quota shape: `SourceResult.quota["codex"]` is a flat dict of numbers:
`{"5h": pct, "5h_resets_in_s": s, "wk": pct, "wk_resets_in_s": s, "as_of": epoch, "stale": 0|1}`.
A reading older than `stale_after_s` keeps its original `as_of` and gets `stale` 1; it is never
dropped. When the file is missing or unreadable the result is status "absent" and no quota.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from datetime import datetime
from pathlib import Path

from ..source import Ctx, SourceResult

NAME = "codex_quota"
QUOTA_KEY = "codex"
DEFAULT_STALE_AFTER_S = 1800.0
TAIL_BYTES = 64 * 1024  # the only part of the file that is read
MAX_LINE_BYTES = 256 * 1024
MAX_JSON_DEPTH = 12  # a real record nests about five levels
MAX_WALK_ENTRIES = 20000  # directory entries looked at per pass
MAX_WALK_DEPTH = 5  # sessions/YYYY/MM/DD/file is 4
MAX_WINDOW_MINUTES = 60 * 24 * 366
MAX_RESETS_AHEAD_S = 400 * 86400
MAX_FUTURE_SKEW_S = 300.0  # a record stamped further ahead than this is not trusted
_ROLLOUT = re.compile(r"rollout-[A-Za-z0-9._-]{1,200}\.jsonl")
_DIGITS = re.compile(r"[0-9]{1,6}")
_TS = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
_MARK = b'"rate_limits"'
_O_DIR = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIR_FLAGS = os.O_RDONLY | _O_DIR | _O_NOFOLLOW | _O_CLOEXEC
_LABELS = {300: "5h", 10080: "wk"}


def _euid() -> int:
    return os.geteuid()


def _no_constant(name: str) -> object:
    raise ValueError("constant")


def _too_deep(line: bytes) -> bool:
    """True when brackets nest deeper than MAX_JSON_DEPTH. Iterative; strings are skipped."""
    depth = 0
    in_str = False
    esc = False
    for b in line:
        if in_str:
            if esc:
                esc = False
            elif b == 0x5C:  # backslash
                esc = True
            elif b == 0x22:  # quote
                in_str = False
        elif b == 0x22:
            in_str = True
        elif b in (0x7B, 0x5B):  # { [
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return True
        elif b in (0x7D, 0x5D):  # } ]
            depth -= 1
    return False


def _num(v: object) -> float | None:
    """A finite int or float (never a bool, never a huge int), else None."""
    if type(v) is int:
        return float(v) if -(2**53) < v < 2**53 else None
    if type(v) is float and math.isfinite(v):
        return v
    return None


def _window(w: object, now: float) -> tuple[str, float, float | None] | None:
    """(label, percent 0..100, resets_in_s or None) from one window object, else None."""
    if type(w) is not dict:
        return None
    mins = w.get("window_minutes")
    used = _num(w.get("used_percent"))
    if type(mins) is not int or not 0 < mins <= MAX_WINDOW_MINUTES or used is None:
        return None
    pct = min(100.0, max(0.0, used))
    left = None
    at = _num(w.get("resets_at"))
    if at is not None and 0 < at - now <= MAX_RESETS_AHEAD_S:
        left = float(int(at - now))
    return _LABELS.get(mins, f"{mins}m"), pct, left


def _stamp(v: object) -> float | None:
    if type(v) is not str or len(v) > 40 or _TS.fullmatch(v) is None:
        return None
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    ts = dt.timestamp()
    return ts if math.isfinite(ts) and ts > 0 else None


def parse_tail(data: bytes, now: float, *, partial_first: bool) -> tuple[float | None, dict] | None:
    """The newest usable rate-limit record in `data`: (record time or None, {label: (pct, left)}).

    Walks lines from the end. `partial_first`: the first line may start mid-line, so it is
    skipped. Returns None when no line carries a usable window. Never raises.
    """
    lines = data.split(b"\n")
    if partial_first and lines:
        lines = lines[1:]
    for raw in reversed(lines):
        if _MARK not in raw or len(raw) > MAX_LINE_BYTES or _too_deep(raw):
            continue
        try:
            doc = json.loads(raw, parse_constant=_no_constant)
        except (ValueError, RecursionError):
            continue
        found = _extract(doc, now)
        if found is not None:
            return found
    return None


def _extract(doc: object, now: float) -> tuple[float | None, dict] | None:
    if type(doc) is not dict:
        return None
    payload = doc.get("payload")
    if type(payload) is not dict:
        return None
    limits = payload.get("rate_limits")
    if type(limits) is not dict:
        return None
    wins: dict[str, tuple[float, float | None]] = {}
    for key in ("primary", "secondary"):
        got = _window(limits.get(key), now)
        if got is not None:
            wins[got[0]] = (got[1], got[2])
    if not wins:
        return None
    return _stamp(doc.get("timestamp")), wins


def _walk_newest(root_fd: int) -> tuple[tuple[str, ...], str] | None:
    """(directory names below root, file name) of the newest regular rollout file, by lstat only.

    Directories are entered through descriptors with no-follow and every descriptor opened here
    is closed before returning. No rollout file is opened.
    """
    best: tuple[tuple[str, ...], str] | None = None
    best_mt = -1
    budget = MAX_WALK_ENTRIES
    # (fd, names below root, owned): the root descriptor belongs to the caller.
    stack: list[tuple[int, tuple[str, ...], bool]] = [(root_fd, (), False)]
    try:
        while stack:
            fd, rel, owned = stack.pop()
            try:
                with os.scandir(fd) as it:
                    for entry in it:
                        budget -= 1
                        if budget < 0:
                            break
                        name = entry.name
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if stat.S_ISDIR(st.st_mode):
                            if len(rel) + 1 >= MAX_WALK_DEPTH or _DIGITS.fullmatch(name) is None:
                                continue
                            try:
                                sub = os.open(name, _DIR_FLAGS, dir_fd=fd)
                            except OSError:
                                continue
                            stack.append((sub, (*rel, name), True))
                        elif (
                            stat.S_ISREG(st.st_mode)
                            and _ROLLOUT.fullmatch(name) is not None
                            and st.st_mtime_ns > best_mt
                        ):
                            best_mt = st.st_mtime_ns
                            best = (rel, name)
            except OSError:
                pass
            finally:
                if owned:
                    _close(fd)
            if budget < 0:
                break
    finally:
        for fd, _rel, owned in stack:
            if owned:
                _close(fd)
    return best


def _close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _open_dir_chain(root_fd: int, rel: tuple[str, ...]) -> int | None:
    """Reopen the directory below root, one no-follow step per name. Caller closes the result."""
    cur = os.dup(root_fd)
    for name in rel:
        try:
            nxt = os.open(name, _DIR_FLAGS, dir_fd=cur)
        except OSError:
            _close(cur)
            return None
        _close(cur)
        cur = nxt
    return cur


def _read_tail(dir_fd: int, name: str) -> tuple[bytes, bool, float] | None:
    """(last TAIL_BYTES bytes, started_mid_file, mtime) of one file, opened once, else None."""
    try:
        fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != _euid():
            return None
        start = max(0, st.st_size - TAIL_BYTES)
        buf = bytearray()
        while len(buf) < TAIL_BYTES:
            chunk = os.pread(fd, TAIL_BYTES - len(buf), start + len(buf))
            if not chunk:
                break
            buf += chunk
        return bytes(buf), start > 0, st.st_mtime
    except OSError:
        return None
    finally:
        _close(fd)


class CodexQuotaSource:
    name = NAME

    def __init__(self, codex_home: Path, stale_after_s: float = DEFAULT_STALE_AFTER_S) -> None:
        self.codex_home = Path(codex_home)
        self.stale_after_s = float(stale_after_s)

    def collect(self, ctx: Ctx) -> SourceResult:
        reason = self._read(ctx)
        if isinstance(reason, str):
            ctx.log.debug("codex_quota: %s", reason)  # a fixed reason word, never file content
            return SourceResult(status="absent")
        return reason

    def _read(self, ctx: Ctx) -> SourceResult | str:
        now = ctx.clock()
        try:
            home_fd = os.open(self.codex_home, os.O_RDONLY | _O_DIR | _O_CLOEXEC)
        except OSError:
            return "no-codex-home"
        try:
            try:
                root = os.open("sessions", _DIR_FLAGS, dir_fd=home_fd)
            except OSError:
                return "no-sessions-dir"
        finally:
            _close(home_fd)
        try:
            newest = _walk_newest(root)
            if newest is None:
                return "no-rollout"
            rel, name = newest
            dir_fd = _open_dir_chain(root, rel)
        finally:
            _close(root)
        if dir_fd is None:
            return "unreadable"
        try:
            got = _read_tail(dir_fd, name)
        finally:
            _close(dir_fd)
        if got is None:
            return "unreadable"
        data, partial, mtime = got
        parsed = parse_tail(data, now, partial_first=partial)
        del data
        if parsed is None:
            return "no-rate-limits"
        stamp, wins = parsed
        as_of = stamp if stamp is not None and stamp <= now + MAX_FUTURE_SKEW_S else mtime
        as_of = min(as_of, now)
        quota: dict[str, float] = {}
        for label, (pct, left) in wins.items():
            quota[label] = pct
            if left is not None:
                quota[f"{label}_resets_in_s"] = left
        quota["as_of"] = float(int(as_of))
        quota["stale"] = 1 if now - as_of > self.stale_after_s else 0
        return SourceResult(quota={QUOTA_KEY: quota})
