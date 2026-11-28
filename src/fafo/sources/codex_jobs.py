"""Codex plugin job files: running jobs and jobs finished within a window.

The plugin keeps one small JSON file per job, `<state>/<id>/jobs/<job>.json`. `jobs_dir` is
the folder that holds either those `<id>/jobs/` folders (one level, the documented layout) or the
`*.json` files directly. Keys read, all optional except status: status, phase, kindLabel, pid,
title, startedAt, completedAt. Everything else in a file is ignored.

The files are untrusted bytes. Names come from one directory listing (capped), each candidate is
opened with no-follow relative to its directory descriptor and judged on the descriptor
(regular file, owned by this user, not group or world writable, at most 64 KB). The JSON is
scanned for nesting depth before it is parsed, refuses duplicate keys and NaN, and is checked
against a fixed schema. A file that fails any of this is counted as invalid and never shown.

Titles may hold prompt text, so they are hidden unless `show_titles` is set; shown titles are
credential-masked, stripped of control characters and cut to 60 characters. Titles are never
logged. Counts (running, done, failed) are always produced.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from datetime import UTC, datetime

from fafo.model import JobRow, JobSummary
from fafo.redact import mask_credentials, sanitize_text
from fafo.source import Ctx, SourceResult

MAX_ENTRIES = 512  # directory entries examined, over the whole layout
MAX_COUNT_EXTRA = 4096  # entries counted (not examined) beyond MAX_ENTRIES
MAX_READ = 128  # files opened per collect, newest first
MAX_ROWS = 64  # rows kept in the snapshot
MAX_FILE_BYTES = 64 * 1024
MAX_DEPTH = 8
MAX_TITLE = 60
MAX_TITLE_RAW = 400
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
    return sanitize_text(mask_credentials(raw[:MAX_TITLE_RAW]), MAX_TITLE)


def _list_dir(fd: int, budget: int) -> tuple[list[tuple[str, int, float, bool]], int]:
    """(name, mode, mtime, is_dir) per entry, lstat only, at most `budget`; and the count of
    entries seen beyond the budget (itself bounded)."""
    out: list[tuple[str, int, float, bool]] = []
    extra = 0
    with os.scandir(fd) as it:
        for entry in it:
            if len(out) >= budget:
                extra += 1
                if extra >= MAX_COUNT_EXTRA:
                    break
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            out.append((entry.name, st.st_mode, st.st_mtime, stat.S_ISDIR(st.st_mode)))
    return out, extra


def _open_dir(name: str, dir_fd: int | None) -> int:
    if dir_fd is None:
        return os.open(name, _DIR_FLAGS)
    return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)


def _candidates(root: str) -> tuple[list[tuple[float, int, str]], int, list[int]]:
    """(mtime, directory descriptor, file name) for each candidate *.json, newest first, the
    number of entries not examined, and the open descriptors (the caller closes them)."""
    found: list[tuple[float, int, str]] = []
    extra = 0
    opened: list[int] = []
    try:
        root_fd = _open_dir(root, None)
        opened.append(root_fd)
        entries, extra = _list_dir(root_fd, MAX_ENTRIES)
        budget = MAX_ENTRIES - len(entries)
        for name, mode, mtime, is_dir in entries:
            if stat.S_ISREG(mode) and name.endswith(".json"):
                found.append((mtime, root_fd, name))
            elif is_dir and budget > 0:
                try:
                    sub = _open_dir(name, root_fd)
                    opened.append(sub)
                    jobs = _open_dir("jobs", sub)
                    opened.append(jobs)
                except OSError:
                    continue
                sub_entries, sub_extra = _list_dir(jobs, budget)
                budget -= len(sub_entries)
                extra += sub_extra
                found.extend(
                    (m, jobs, n)
                    for n, md, m, d in sub_entries
                    if stat.S_ISREG(md) and n.endswith(".json")
                )
    except BaseException:
        for fd in opened:
            os.close(fd)
        raise
    found.sort(key=lambda t: t[0], reverse=True)
    return found, extra, opened


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
            found, extra, fds = _candidates(self.jobs_dir)
        except OSError:
            return SourceResult(status="absent")
        counts = {"running": 0, "done": 0, "failed": 0}
        invalid = 0
        rows: list[tuple[int, float, JobRow]] = []
        try:
            for _mtime, dfd, fname in found[:MAX_READ]:
                try:
                    fd = os.open(fname, _FILE_FLAGS, dir_fd=dfd)
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
            for fd in fds:
                os.close(fd)
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
