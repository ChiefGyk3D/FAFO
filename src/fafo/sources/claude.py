"""Claude source: live sessions from the registry directory, joined to their processes.

Reads only files named <digits>.json. The registry JSON carries a peer token and a
messaging socket path; both are dropped right after parsing and never leave this module.
Transcripts and anything under the projects directory are never read.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Protocol

import psutil

from ..attribution import GitRunner, attribute
from ..model import Proc, Session, Stamped
from ..redact import mask_credentials, redact_argv, sanitize_text
from ..source import Ctx, SourceResult

DEFAULT_DIR = Path("~/.claude/sessions")
MAX_FILE_BYTES = 64 * 1024
MAX_ENTRIES = 256  # registry files processed per cycle, newest first
MAX_SCANNED = 65536  # directory entries examined (stat of names only) before ignoring the rest
MAX_CHILDREN = 256  # descendants reported per session
MAX_DEPTH = 8
MAX_NAME = 80
MAX_TASK = 80
MAX_CWD = 200
MAX_ID = 128
STAT_CAP = 4096
_NAME_RE = re.compile(r"[0-9]{1,10}\.json")
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TICKS_RE = re.compile(r"[0-9]{1,20}")
_STATES = {"busy", "idle", "waiting"}
_AGENTS = {"codex", "gemini", "ollama", "claude"}
# The only registry fields ever read. Anything else (peerToken, messagingSocketPath, ...) is
# dropped at parse time.
_DROP = ("peerToken", "messagingSocketPath")
_KEEP = ("pid", "sessionId", "cwd", "startedAt", "procStart", "name", "status", "waitingFor")
# FAFO#15 adds a configured value-flags list; until then this fixed set applies.
VALUE_FLAGS = frozenset({"--model", "--resume"})


class ProcReader(Protocol):
    def start_ticks(self, pid: int) -> str | None:
        """Clock-tick start time (/proc/<pid>/stat field 22) as a string, None if no such process."""


def parse_start_ticks(text: str) -> str | None:
    """Field 22 of a /proc/<pid>/stat line. The command name may contain spaces and parens."""
    end = text.rfind(")")
    if end < 0:
        return None
    rest = text[end + 1 :].split()
    if len(rest) < 20:
        return None
    ticks = rest[19]  # field 22; rest starts at field 3
    return ticks if _TICKS_RE.fullmatch(ticks) else None


class ProcFsReader:
    def start_ticks(self, pid: int) -> str | None:
        try:
            fd = os.open(f"/proc/{int(pid)}/stat", os.O_RDONLY | os.O_CLOEXEC)
        except (OSError, ValueError):
            return None
        try:
            return parse_start_ticks(os.read(fd, STAT_CAP).decode("utf-8", errors="replace"))
        except OSError:
            return None
        finally:
            os.close(fd)


class _DupKey(ValueError):
    pass


def _no_dupes(pairs: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise _DupKey(k)
        out[k] = v
    return out


def _depth_ok(value: Any) -> bool:
    """Iterative nesting check, so a hostile document cannot recurse us."""
    stack = [(value, 1)]
    while stack:
        cur, depth = stack.pop()
        if depth > MAX_DEPTH:
            return False
        if isinstance(cur, dict):
            stack.extend((v, depth + 1) for v in cur.values())
        elif isinstance(cur, list):
            stack.extend((v, depth + 1) for v in cur)
    return True


def _read_file(dir_fd: int, name: str) -> bytes | None:
    """Read one registry file by name relative to the directory descriptor, size capped."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
            return None
        data = os.read(fd, MAX_FILE_BYTES + 1)
        return data if len(data) <= MAX_FILE_BYTES else None
    except OSError:
        return None
    finally:
        os.close(fd)


def _parse(data: bytes) -> dict | None:
    try:
        doc = json.loads(data.decode("utf-8"), object_pairs_hook=_no_dupes)
    except (ValueError, RecursionError):  # includes UnicodeDecodeError and duplicate keys
        return None
    if type(doc) is not dict or not _depth_ok(doc):
        return None
    # Drop the secrets first, then keep only the fields this source uses.
    for key in _DROP:
        doc.pop(key, None)
    return {k: doc[k] for k in _KEEP if k in doc}


def load_registry_file(directory: Path | int, name: str) -> dict | None:
    """Parsed, secret-free record for one `<digits>.json`, or None if unusable.

    `directory` is a path or an already open directory descriptor.
    """
    if _NAME_RE.fullmatch(name) is None:
        return None
    if isinstance(directory, int):
        return _load(directory, name)
    try:
        dir_fd = os.open(os.fspath(directory), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        return _load(dir_fd, name)
    finally:
        os.close(dir_fd)


def _load(dir_fd: int, name: str) -> dict | None:
    data = _read_file(dir_fd, name)
    return None if data is None else _parse(data)


def _num(value: Any) -> float | None:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _label(value: Any, cap: int) -> str:
    if type(value) is not str:
        return ""
    return sanitize_text(mask_credentials(value), cap)


def _ppid(p: Any) -> int | None:
    try:
        return int(p.ppid())
    except Exception:  # noqa: BLE001 - the process vanished mid-read
        return None


def _agent_of(argv: list[str], name: str) -> str:
    base = os.path.basename(argv[0]) if argv else name
    low = base.lower()
    return low if low in _AGENTS else "other"


class ClaudeSource:
    name = "claude"

    def __init__(
        self,
        sessions_dir: Path | str | None = None,
        *,
        show_args: set[str] | None = None,
        proc_reader: ProcReader | None = None,
        psutil_mod: Any = psutil,
        git: GitRunner | None = None,
    ) -> None:
        self.dir = Path(sessions_dir if sessions_dir is not None else DEFAULT_DIR).expanduser()
        self.show_args = set(show_args or ())
        self.procs: ProcReader = proc_reader or ProcFsReader()
        self.ps = psutil_mod
        self.git = git

    # -- registry ------------------------------------------------------------------------
    def _names(self, dir_fd: int) -> list[str]:
        """Registry file names, newest first, at most MAX_ENTRIES. Other names are never touched."""
        found: list[tuple[float, str]] = []
        scanned = 0
        with os.scandir(dir_fd) as it:
            for entry in it:
                scanned += 1
                if scanned > MAX_SCANNED:
                    break
                if _NAME_RE.fullmatch(entry.name) is None:
                    continue
                try:
                    mtime = entry.stat(follow_symlinks=False).st_mtime
                except OSError:
                    continue
                found.append((mtime, entry.name))
        found.sort(reverse=True)
        return [n for _, n in found[:MAX_ENTRIES]]

    def _live_entries(self, ctx: Ctx) -> list[dict] | None:
        try:
            dir_fd = os.open(os.fspath(self.dir), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError:
            return None
        entries: list[dict] = []
        try:
            for name in self._names(dir_fd):
                rec = load_registry_file(dir_fd, name)
                if rec is None:
                    ctx.log.debug("claude: skipped an unreadable registry file")
                    continue
                entry = self._validate(rec, int(name[:-5]))
                if entry is not None:
                    entries.append(entry)
        finally:
            os.close(dir_fd)
        return entries

    def _validate(self, rec: dict, file_pid: int) -> dict | None:
        pid = rec.get("pid")
        sid = rec.get("sessionId")
        ticks = rec.get("procStart")
        started = _num(rec.get("startedAt"))
        if type(pid) is not int or pid != file_pid or pid <= 0:
            return None
        if type(sid) is not str or _ID_RE.fullmatch(sid) is None:
            return None
        if type(ticks) is not str or _TICKS_RE.fullmatch(ticks) is None or started is None:
            return None
        # PID reuse: the process must exist and have the recorded start time.
        if self.procs.start_ticks(pid) != ticks:
            return None
        return {**rec, "started_s": started / 1000.0}

    # -- processes -----------------------------------------------------------------------
    def _children(self, session_pid: int, live: set[int]) -> list[tuple[Proc, str | None]]:
        try:
            root = self.ps.Process(session_pid)
            kids = list(root.children(recursive=True))
        except Exception:  # noqa: BLE001 - psutil errors vary; a vanished process drops the rows
            return []
        parent = {k.pid: pp for k in kids if (pp := _ppid(k)) is not None}
        out: list[tuple[Proc, str | None]] = []
        for k in kids:
            if len(out) >= MAX_CHILDREN:
                break
            if k.pid in live or k.pid not in parent:
                continue
            if self._owner(k.pid, parent, live, session_pid) != session_pid:
                continue
            row = self._proc_row(k, session_pid)
            if row is not None:
                out.append(row)
        return out

    @staticmethod
    def _owner(pid: int, parent: dict[int, int], live: set[int], root: int) -> int | None:
        """Nearest ancestor that is itself a registered session, bounded walk."""
        cur = parent.get(pid)
        for _ in range(len(parent) + 1):
            if cur is None:
                return None
            if cur in live:
                return cur
            if cur == root:
                return root
            cur = parent.get(cur)
        return None

    def _proc_row(self, p: Any, session_pid: int) -> tuple[Proc, str | None] | None:
        try:
            argv = [a for a in p.cmdline() if isinstance(a, str)]
            try:
                cwd: str | None = p.cwd()
            except Exception:  # noqa: BLE001 - cwd is optional (deleted or no permission)
                cwd = None
            row = Proc(
                pid=int(p.pid),
                ppid=int(p.ppid()),
                session_pid=session_pid,
                agent=_agent_of(argv, p.name()),
                cmd=redact_argv(argv, self.show_args, value_flags=VALUE_FLAGS),
                cpu=max(0.0, float(p.cpu_percent(None))),
                rss=max(0, int(p.memory_info().rss)),
                started=max(0.0, float(p.create_time())),
            )
        except Exception:  # noqa: BLE001 - one vanished or odd process never fails the source
            return None
        return row, cwd

    def _own_usage(self, pid: int) -> tuple[float, int] | None:
        try:
            p = self.ps.Process(pid)
            return max(0.0, float(p.cpu_percent(None))), max(0, int(p.memory_info().rss))
        except Exception:  # noqa: BLE001 - the process exited between the check and now
            return None

    # -- collect -------------------------------------------------------------------------
    def collect(self, ctx: Ctx) -> SourceResult:
        entries = self._live_entries(ctx)
        if entries is None:
            return SourceResult(status="absent")
        now = ctx.clock()
        live = {e["pid"] for e in entries}
        result = SourceResult()
        for e in entries:
            try:
                self._add(result, e, live, now)
            except Exception:  # noqa: BLE001 - any single bad entry is skipped
                ctx.log.debug("claude: skipped an entry that failed")
        return result

    def _add(self, result: SourceResult, e: dict, live: set[int], now: float) -> None:
        pid = e["pid"]
        usage = self._own_usage(pid)
        if usage is None:
            return
        kids = self._children(pid, live)
        cwd = e.get("cwd") if type(e.get("cwd")) is str else None
        child_cwds = [c for _, c in kids if c]
        kw: dict[str, Any] = {"now": time.monotonic}
        if self.git is not None:
            kw["git"] = self.git
        project, rule = attribute(cwd, child_cwds, **kw)
        status = e.get("status")
        state = status if type(status) is str and status in _STATES else "unknown"
        waiting = _label(e.get("waitingFor"), MAX_TASK)
        task = waiting or (state if state in _STATES else "")
        sid = e["sessionId"]
        result.sessions.append(
            Session(
                pid=pid,
                agent="claude",
                session_id=sid,
                project=Stamped(project, now),
                project_rule=rule,
                task=task,
                state=state,
                branch="",
                worktree=sanitize_text(cwd, MAX_CWD) if cwd else "",
                started=e["started_s"],
                cpu=usage[0],
                rss=usage[1],
                name=_label(e.get("name"), MAX_NAME),
                address=sid,
            )
        )
        result.procs.extend(row for row, _ in kids)

