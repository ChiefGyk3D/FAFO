"""Claude quota source: the 5-hour and weekly usage windows from a cache file.

This reads a file another tool writes (for example a status line that sees Claude's rate
limit data). FAFO never writes it and never asks Claude for the numbers. The file is JSON:

    {"windows": [{"label": "5h", "used_percent": 41.4, "resets_at": 1800007800,
                  "captured_at": 1799999940}, {"label": "weekly", ...}]}

`label` is "5h" or "weekly" (other labels are ignored), `used_percent` is a number,
`resets_at` is epoch seconds or null, `captured_at` is epoch seconds. Exactly those four keys
per window and exactly the top-level key `windows`; anything else is refused.

The file is untrusted input. It is opened by descriptor with no-follow, must be a regular file
owned by the effective user with no group or other permission bits (its directory must be owned
by that user and not group or other writable), is capped at 64 KB, nested at most a few levels,
and every number must be finite. A percent is clamped to 0..100 after a sanity check that it
lies within -1..200. The cache's own `captured_at` decides freshness: older than 30 minutes
(the same cutoff the dispatch guard and the Codex quota reader use) the window is marked stale
and keeps its original `as_of`, so it dims and never vanishes.

A missing file is status `absent`. An unreadable, refused, oversized, corrupt or
schema-invalid file raises QuotaCacheError whose text is one of REASONS and nothing else: no
path, no value and no file text reaches an error or a log line.
"""

from __future__ import annotations

import contextlib
import errno
import json
import math
import os
import stat
from pathlib import Path

from ..source import Ctx, SourceResult

NAME = "claude_quota"
QUOTA_KEY = "claude"
MAX_BYTES = 64 * 1024
STALE_AFTER_S = 30 * 60
SKEW_S = 300.0  # a capture time this far ahead of the clock is tolerated, more is refused
MAX_DEPTH = 6
MAX_WINDOWS = 8
PCT_MIN = -1.0  # below or above these the number is not a percent at all: refuse, do not clamp
PCT_MAX = 200.0
MAX_RESET_HORIZON_S = 30 * 86400.0
MAX_INT_DIGITS = 20
MAX_EXACT = 2**53
LABELS = {"5h": "five_hour", "weekly": "weekly"}
WINDOW_KEYS = frozenset({"label", "used_percent", "resets_at", "captured_at"})
REASONS = ("unreadable", "refused", "too_large", "corrupt", "schema")

BAD_MODE_BITS = stat.S_IRWXG | stat.S_IRWXO
_DIR_BAD_BITS = stat.S_IWGRP | stat.S_IWOTH
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
_MISSING = (errno.ENOENT, errno.ENOTDIR)


class QuotaCacheError(Exception):
    """The cache cannot be used. The text is always one of REASONS."""

    def __init__(self, reason: str) -> None:
        if reason not in REASONS:
            raise ValueError("QuotaCacheError takes a reason word only")
        super().__init__(reason)


def _read(path: Path) -> bytes | None:
    """The file's bytes, None when it does not exist, QuotaCacheError for anything else."""
    name = path.name
    if name in ("", ".", ".."):
        raise QuotaCacheError("refused")
    uid = os.geteuid()
    dfd = -1
    fd = -1
    try:
        try:
            dfd = os.open(os.fspath(path.parent), _DIR_FLAGS)  # the configured root may be a link
            dst = os.fstat(dfd)
            if dst.st_uid != uid or stat.S_IMODE(dst.st_mode) & _DIR_BAD_BITS:
                raise QuotaCacheError("refused")
            fd = os.open(name, _FILE_FLAGS, dir_fd=dfd)  # a link as the last part is refused
        except OSError as exc:
            if exc.errno in _MISSING:  # O_NOFOLLOW gives ELOOP for a link, never ENOENT
                return None
            raise QuotaCacheError("unreadable") from None
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != uid
            or stat.S_IMODE(st.st_mode) & BAD_MODE_BITS
        ):
            raise QuotaCacheError("refused")
        if st.st_size > MAX_BYTES:
            raise QuotaCacheError("too_large")
        chunks: list[bytes] = []
        total = 0
        try:
            while total <= MAX_BYTES:
                chunk = os.read(fd, MAX_BYTES + 1 - total)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
                total += len(chunk)
        except OSError:
            raise QuotaCacheError("unreadable") from None
        raise QuotaCacheError("too_large")
    finally:
        for d in (fd, dfd):
            if d != -1:
                with contextlib.suppress(OSError):
                    os.close(d)


def _too_deep(data: bytes) -> bool:
    """Nesting deeper than MAX_DEPTH, counted iteratively and outside strings."""
    depth = 0
    in_str = False
    esc = False
    for b in data:
        if in_str:
            if esc:
                esc = False
            elif b == 0x5C:
                esc = True
            elif b == 0x22:
                in_str = False
        elif b == 0x22:
            in_str = True
        elif b in (0x5B, 0x7B):
            depth += 1
            if depth > MAX_DEPTH:
                return True
        elif b in (0x5D, 0x7D):
            depth -= 1
    return False


def _no_constant(_name: str):
    raise ValueError("non-finite constant")


def _parse_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value


def _parse_int(text: str) -> int:
    if len(text) > MAX_INT_DIGITS:
        raise ValueError("huge integer")
    return int(text)


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def _parse(data: bytes) -> object:
    if _too_deep(data):
        raise QuotaCacheError("corrupt")
    try:
        return json.loads(
            data.decode("utf-8"),
            parse_constant=_no_constant,
            parse_float=_parse_float,
            parse_int=_parse_int,
            object_pairs_hook=_pairs,
        )
    except (ValueError, RecursionError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise QuotaCacheError("corrupt") from None


def _num(value: object) -> float | None:
    """An exact int or float that is finite and small enough to be exact, else None."""
    t = type(value)
    if t is not int and t is not float:
        return None
    if t is int:
        if abs(value) >= MAX_EXACT:  # type: ignore[arg-type]
            return None
        return float(value)  # type: ignore[arg-type]
    return value if math.isfinite(value) else None  # type: ignore[return-value]


def _window(w: dict, now: float) -> dict:
    used = _num(w["used_percent"])
    cap = _num(w["captured_at"])
    if used is None or not PCT_MIN <= used <= PCT_MAX:
        raise QuotaCacheError("schema")
    if cap is None or not 0 < cap <= now + SKEW_S:
        raise QuotaCacheError("schema")
    reset = w["resets_at"]
    left = None
    if reset is not None:
        r = _num(reset)
        if r is None or not 0 < r <= now + MAX_RESET_HORIZON_S:
            raise QuotaCacheError("schema")
        left = int(r - now) if r > now else None
    return {
        "used_pct": min(100.0, max(0.0, used)),
        "resets_in_s": left,
        "as_of": min(cap, now),
        "stale": now - cap > STALE_AFTER_S,
    }


def _validate(doc: object, now: float) -> dict:
    if type(doc) is not dict or set(doc) != {"windows"}:
        raise QuotaCacheError("schema")
    items = doc["windows"]
    if type(items) is not list or len(items) > MAX_WINDOWS:
        raise QuotaCacheError("schema")
    out: dict = {}
    for w in items:
        if type(w) is not dict or set(w) != WINDOW_KEYS:
            raise QuotaCacheError("schema")
        label = w["label"]
        if type(label) is not str:
            raise QuotaCacheError("schema")
        key = LABELS.get(label)
        if key is None:
            continue
        if key in out:
            raise QuotaCacheError("schema")
        out[key] = _window(w, now)
    return out


class ClaudeQuotaSource:
    name = NAME

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def collect(self, ctx: Ctx) -> SourceResult:
        try:
            data = _read(self._path)
            if data is None:
                return SourceResult(status="absent")
            windows = _validate(_parse(data), float(ctx.clock()))
        except QuotaCacheError as exc:
            ctx.log.debug("claude_quota: cache refused (%s)", str(exc))
            raise QuotaCacheError(str(exc)) from None
        if not windows:
            return SourceResult(status="absent")
        return SourceResult(quota={QUOTA_KEY: windows})
