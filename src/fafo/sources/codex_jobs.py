"""Codex plugin job files: running jobs and jobs finished within a window.

The plugin keeps one small JSON file per job, `<state>/<id>/jobs/<job>.json`. `jobs_dir` is
the folder that holds either those `<id>/jobs/` folders (one level, the documented layout) or the
`*.json` files directly. Keys read, all optional except status: status, phase, kindLabel, pid,
title, startedAt, completedAt. Everything else in a file is ignored.

The files are untrusted bytes. Names come from one directory listing (capped), each candidate is
opened with no-follow relative to its directory descriptor and judged on the descriptor
(regular file, owned by this user, not group or world writable, at most 64 KB). The JSON is
scanned for nesting depth before it is parsed, refuses duplicate keys and NaN, and is checked
against a fixed schema. A file that fails any of this is counted as invalid and never shown. A
`*.json` entry that is not a regular file (symlink, FIFO, directory, device) is counted as invalid
from its listing and is never opened.

Titles may hold prompt text, so they are hidden unless `show_titles` is set. Shown titles are
free text and masking is best effort: it covers token shapes, `key=value` and flag-value forms and
keyword phrases, with controls and separators treated as word breaks. It does not cover a bare
secret word followed by a plain value, nor a secret flag split by a terminal escape sequence.
Shown titles are normalized over the whole title, credential-masked, and cut to 60 characters.
Titles are never logged. Counts (running, done, failed) are always produced.

Counting: `invalid` counts every file that fails the checks above, including a `*.json` entry
whose stat fails. `unread` counts entries not examined, and each folder the budget does not reach.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from datetime import UTC, datetime

from fafo.model import JobRow, JobSummary
from fafo.redact import masked_text
from fafo.source import Ctx, SourceResult

MAX_ENTRIES = 512  # directory entries examined, over the whole layout
MAX_JOB_ENTRIES = 128  # directory entries examined inside one `<id>/jobs` folder
MAX_COUNT_EXTRA = 4096  # entries counted (not examined) beyond the caps
MAX_READ = 128  # files opened per collect, newest first
MAX_ROWS = 64  # rows kept in the snapshot
MAX_FILE_BYTES = 64 * 1024
MAX_DEPTH = 8
MAX_TITLE = 60
MAX_LABEL = 24
DEFAULT_WINDOW_MIN = 60
MAX_WINDOW_MIN = 1440
MAX_PID = 4_194_304
SKEW_S = 60.0
_EARLIEST = datetime(2000, 1, 1, tzinfo=UTC).timestamp()
_LATEST = datetime(2100, 1, 1, tzinfo=UTC).timestamp()
_STATUSES = frozenset({"queued", "running", "completed", "failed", "cancelled"})
_LABEL = re.compile(rf"[A-Za-z0-9][A-Za-z0-9 _-]{{0,{MAX_LABEL - 1}}}")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_OPEN_STATES = ("running", "done", "failed")


class _Bad(Exception):
    """A job file that is not acceptable. Never carries file content."""


def _depth_ok(raw: bytes) -> bool:
    """True when brackets nest at most MAX_DEPTH deep, outside strings. Iterative, no recursion."""
    depth = 0
    in_str = esc = False
    for b in raw:
        if in_str:
            if esc:
                esc = False
            elif b == 0x5C:  # backslash
                esc = True
            elif b == 0x22:
                in_str = False
        elif b == 0x22:
            in_str = True
        elif b in (0x5B, 0x7B):  # [ {
            depth += 1
            if depth > MAX_DEPTH:
                return False
        elif b in (0x5D, 0x7D):  # ] }
            depth -= 1
    return True


def _no_constant(_name: str):
    raise ValueError("non-finite number")


def _pairs(items):
    out: dict = {}
    for k, v in items:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def _read_fd(fd: int) -> bytes:
    st = os.fstat(fd)
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_uid != os.geteuid()
        or stat.S_IMODE(st.st_mode) & (stat.S_IWGRP | stat.S_IWOTH)
        or st.st_size > MAX_FILE_BYTES
    ):
        raise _Bad
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_FILE_BYTES:
        chunk = os.read(fd, MAX_FILE_BYTES + 1 - total)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
    raise _Bad


def _epoch(value: object) -> float | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > 64:
        raise _Bad
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        ts = dt.timestamp()
    except (ValueError, OverflowError, OSError):
        raise _Bad from None
    if not math.isfinite(ts) or not _EARLIEST <= ts <= _LATEST:
        raise _Bad
    return ts


def _opt_str(doc: dict, key: str) -> str:
    v = doc.get(key)
    if v is None:
        return ""
    if type(v) is not str:
        raise _Bad
    return v


def _label(text: str) -> str:
    """Plugin labels (phase, kind) are shown only when they look like a short plain label."""
    return text if _LABEL.fullmatch(text) else ""


def _parse(raw: bytes) -> dict:
    if not _depth_ok(raw):
        raise _Bad
    try:
        doc = json.loads(raw, parse_constant=_no_constant, object_pairs_hook=_pairs)
    except (ValueError, RecursionError, UnicodeDecodeError):
        raise _Bad from None
    if type(doc) is not dict:
        raise _Bad
    status = doc.get("status")
    if type(status) is not str or status not in _STATUSES:
        raise _Bad
    pid = doc.get("pid")
    if pid is not None and (type(pid) is not int or not 0 < pid <= MAX_PID):
        raise _Bad
    return {
        "status": status,
        "pid": pid,
        "phase": _label(_opt_str(doc, "phase")),
        "kind": _label(_opt_str(doc, "kindLabel")),
        "title": _opt_str(doc, "title"),
        "started": _epoch(doc.get("startedAt")),
        "completed": _epoch(doc.get("completedAt")),
    }


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, belongs to someone else
    except OSError:
        return False
    return True


def _title(raw: str) -> str:
    """Strip control characters over the whole title (the file cap bounds it), then mask, then
    cut. The order matters: the masker reads a bounded prefix, so controls must go first."""
    return masked_text(raw, MAX_TITLE, MAX_FILE_BYTES)


def _scan(
    fd: int, cap: int, extra_cap: int
) -> tuple[list[tuple[str, int, float, bool]], int, int, int]:
    """Entries of one folder: (name, mode, mtime, is_dir) for each entry examined (lstat only),
    the number examined, the number seen beyond the cap (at most extra_cap), and the number of
    `*.json` entries whose stat failed (they cannot be judged, so the caller counts them invalid).

    Every entry pulled from scandir counts against the cap before its stat is tried, so entries
    whose stat fails still use the budget. The loop stops once both budgets are spent."""
    out: list[tuple[str, int, float, bool]] = []
    examined = 0
    extra = 0
    unstatable = 0
    with os.scandir(fd) as listing:
        feed = iter(listing)
        while examined < cap or extra < extra_cap:
            entry = next(feed, None)
            if entry is None:
                break
            if examined >= cap:
                extra += 1
                continue
            examined += 1
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                if entry.name.endswith(".json"):
                    unstatable += 1
                continue
            out.append((entry.name, st.st_mode, st.st_mtime, stat.S_ISDIR(st.st_mode)))
    return out, examined, extra, unstatable


def _open_dir(name: str, dir_fd: int | None) -> int:
    if dir_fd is None:
        return os.open(name, _DIR_FLAGS)
    return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)


def _open_jobs(root_fd: int, name: str) -> int:
    """Descriptor of `<name>/jobs`, opened relative to root_fd. The `<name>` descriptor is closed
    before return, so only the jobs descriptor is handed back (the caller closes it)."""
    sub_fd = _open_dir(name, root_fd)
    try:
        return _open_dir("jobs", sub_fd)
    finally:
        os.close(sub_fd)


def _open_job(root_fd: int, sub: str, name: str) -> int:
    """No-follow open of one candidate file. Any folder descriptor used on the way is closed."""
    if not sub:
        return os.open(name, _FILE_FLAGS, dir_fd=root_fd)
    jobs_fd = _open_jobs(root_fd, sub)
    try:
        return os.open(name, _FILE_FLAGS, dir_fd=jobs_fd)
    finally:
        os.close(jobs_fd)


def _candidates(root_fd: int) -> tuple[list[tuple[float, str, str]], int, int]:
    """(mtime, subfolder or "", file name) for each candidate *.json, newest first; the number of
    entries not examined; and the number of *.json entries that are not regular files (counted,
    never opened). At most one `<id>/jobs` folder is open at a time, and it is closed before the
    next one is opened."""
    found: list[tuple[float, str, str]] = []
    entries, examined, extra, invalid = _scan(root_fd, MAX_ENTRIES, MAX_COUNT_EXTRA)
    remaining = MAX_ENTRIES - examined
    for name, mode, mtime, is_dir in entries:
        if name.endswith(".json"):
            if stat.S_ISREG(mode):
                found.append((mtime, "", name))
            else:
                invalid += 1
        elif is_dir:
            if remaining <= 0:
                extra += 1  # a folder the budget never reached: its job files are unexamined
                continue
            try:
                jobs_fd = _open_jobs(root_fd, name)
            except FileNotFoundError:
                continue  # no jobs folder: no job files to count
            except OSError:
                extra += 1  # present but unreadable: its job files are unexamined
                continue
            try:
                sub, sub_examined, sub_extra, sub_bad = _scan(
                    jobs_fd, min(remaining, MAX_JOB_ENTRIES), MAX_COUNT_EXTRA - extra
                )
            finally:
                os.close(jobs_fd)
            remaining -= sub_examined
            extra += sub_extra
            invalid += sub_bad
            for sub_name, sub_mode, sub_mtime, _ in sub:
                if sub_name.endswith(".json"):
                    if stat.S_ISREG(sub_mode):
                        found.append((sub_mtime, name, sub_name))
                    else:
                        invalid += 1
    found.sort(key=lambda t: t[0], reverse=True)
    return found, extra, invalid


class CodexJobsSource:
    name = "codex_jobs"

    def __init__(
        self,
        jobs_dir: str,
        window_minutes: int = DEFAULT_WINDOW_MIN,
        show_titles: bool = False,
        pid_alive=_pid_alive,
    ) -> None:
        self.jobs_dir = jobs_dir
        self.window_s = float(window_minutes) * 60.0
        self.show_titles = show_titles
        self._alive = pid_alive

    def _classify(self, job: dict, now: float) -> tuple[str, float] | None:
        status = job["status"]
        if status in ("queued", "running"):
            pid = job["pid"]
            if pid is None or not self._alive(pid):
                return None
            started = job["started"]
            return "running", max(0.0, now - started) if started is not None else 0.0
        done = job["completed"]
        if done is None:
            return None
        age = now - done
        if age < -SKEW_S or age > self.window_s:
            return None
        return ("done" if status == "completed" else "failed"), max(0.0, age)

    def collect(self, ctx: Ctx) -> SourceResult:
        now = ctx.clock()
        try:
            root_fd = _open_dir(self.jobs_dir, None)
        except OSError:
            return SourceResult(status="absent")
        try:
            try:
                found, extra, invalid = _candidates(root_fd)
            except OSError:
                return SourceResult(status="absent")
            counts = {"running": 0, "done": 0, "failed": 0}
            rows: list[tuple[int, float, JobRow]] = []
            for _mtime, sub, fname in found[:MAX_READ]:
                try:
                    fd = _open_job(root_fd, sub, fname)
                except OSError:
                    invalid += 1
                    continue
                try:
                    job = _parse(_read_fd(fd))
                except (_Bad, OSError):
                    invalid += 1
                    continue
                finally:
                    os.close(fd)
                verdict = self._classify(job, now)
                if verdict is None:
                    continue
                state, age = verdict
                counts[state] += 1
                rows.append(
                    (
                        _OPEN_STATES.index(state),
                        age,
                        JobRow(
                            state=state,
                            age=age,
                            kind=job["kind"],
                            phase=job["phase"],
                            title=_title(job["title"]) if self.show_titles else "",
                        ),
                    )
                )
        finally:
            os.close(root_fd)
        rows.sort(key=lambda t: (t[0], t[1]))
        unread = extra + max(0, len(found) - MAX_READ)
        summary = JobSummary(
            running=counts["running"],
            done=counts["done"],
            failed=counts["failed"],
            hidden=max(0, len(rows) - MAX_ROWS),
            invalid=invalid,
            unread=unread,
            rows=[r for _, _, r in rows[:MAX_ROWS]],
        )
        if invalid or unread:
            ctx.log.warning("codex_jobs: %d unreadable files, %d not examined", invalid, unread)
        return SourceResult(jobs=summary)
