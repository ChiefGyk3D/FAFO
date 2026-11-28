"""Codex source: process tree, plus the optional you-are-here files.

Finds Codex processes (`codex`, `codex-code-mode-host`, node running a script
named `codex`) through an injectable lister and attaches each to the nearest
Claude Code ancestor. No Codex session or history file is read here.

Optionally a Codex SessionStart hook (examples/codex/fafo-session-hook.sh) records
`{session_id, ppid, started_at}` in `<state dir>/codex-here/<ppid>.json`. This module reads
that directory (bounded, descriptor-based, no-follow) and, when a record belongs to a live
codex process that started no later than the record, reports that process as a Session whose
address is the Codex session id. The collector never deletes those files.

The lister returns psutil.Process-like objects (pid, ppid(), cmdline(),
cpu_percent(), memory_info(), create_time()). Any of those can fail because the
process exited or is not ours: such a process is skipped, never fatal.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

import psutil

from ..model import Proc, Session, Stamped
from ..proccache import ProcCache
from ..redact import redact_argv, sanitize_text
from ..source import Ctx, SourceResult

MAX_ROWS = 128
MAX_TABLE = 50000  # processes examined per pass
MAX_DEPTH = 64  # ancestor hops
VALUE_FLAGS = frozenset({"--model", "--resume"})
_CODEX_HOSTS = frozenset({"codex-code-mode-host"})

Lister = Callable[[], Iterable[Any]]

MAX_HERE_FILES = 256  # records read per pass
MAX_HERE_SCAN = 4096  # directory entries examined per pass
MAX_HERE_BYTES = 4096  # bytes per record
START_SLACK_S = 2.0  # clock jitter allowed between process start time and the hook's clock
_HERE_NAME = re.compile(r"[1-9][0-9]{0,9}\.json")
_HERE_SID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_HERE_KEYS = frozenset({"session_id", "ppid", "started_at"})
_NAME_ID_CHARS = 8


def _euid() -> int:
    return os.geteuid()


@dataclass(frozen=True)
class HereRecord:
    pid: int
    session_id: str
    started_at: float


class _Dupe(ValueError):
    pass


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in items:
        if k in out:
            raise _Dupe(k)
        out[k] = v
    return out


def _no_constant(name: str) -> Any:
    raise ValueError(name)


def _read_capped(fd: int) -> bytes | None:
    """At most MAX_HERE_BYTES; None when the file holds more (the read itself is bounded)."""
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_HERE_BYTES:
        chunk = os.read(fd, MAX_HERE_BYTES + 1 - total)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
    return None


def _parse_here(data: bytes, name: str) -> HereRecord | None:
    try:
        doc = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_no_constant)
    except (ValueError, RecursionError):  # includes UnicodeDecodeError and duplicate keys
        return None
    if type(doc) is not dict or set(doc) != _HERE_KEYS:
        return None
    sid, ppid, started = doc["session_id"], doc["ppid"], doc["started_at"]
    if type(sid) is not str or not _HERE_SID.fullmatch(sid):
        return None
    if type(ppid) is not int or ppid <= 0 or f"{ppid}.json" != name:
        return None
    if type(started) is not float or not math.isfinite(started) or started < 0:
        return None
    return HereRecord(ppid, sid, started)


def _read_here_file(dfd: int, name: str) -> HereRecord | None:
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd
        )
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != _euid():
            return None
        if st.st_mode & 0o077 or st.st_size > MAX_HERE_BYTES:
            return None
        data = _read_capped(fd)
    except OSError:
        return None
    finally:
        os.close(fd)
    return None if data is None else _parse_here(data, name)


def read_here_dir(directory: Path | str | None) -> list[HereRecord]:
    """Valid records from the hook's directory; anything doubtful is skipped (fail closed).

    The directory is opened without following a final symlink and checked on the descriptor
    (a directory owned by this user with no group or other access); each file is opened
    relative to it, no-follow, and checked on its own descriptor. Read-only: nothing is deleted.
    """
    if directory is None:
        return []
    try:
        dfd = os.open(
            str(directory), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except OSError:
        return []
    try:
        st = os.fstat(dfd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != _euid() or st.st_mode & 0o077:
            return []
        try:
            with os.scandir(dfd) as it:
                names = [e.name for e in islice(it, MAX_HERE_SCAN) if _HERE_NAME.fullmatch(e.name)]
        except OSError:
            return []
        out: list[HereRecord] = []
        for name in sorted(names)[:MAX_HERE_FILES]:
            rec = _read_here_file(dfd, name)
            if rec is not None:
                out.append(rec)
        return out
    finally:
        os.close(dfd)


@dataclass(frozen=True)
class Entry:
    pid: int
    ppid: int
    argv: tuple[str, ...]
    started: float
    handle: Any


def _base(token: str) -> str:
    return token.rsplit("/", 1)[-1]


# Interpreter command lines. Anything not understood is NOT a match (fail closed).
_NODE = frozenset({"node", "nodejs"})
_NODE_VALUE_OPTS = frozenset(
    {
        "-r", "--require", "--import", "--loader", "--experimental-loader", "--input-type",
        "-C", "--conditions", "--title", "--env-file", "--inspect-port", "--disable-warning",
        "--watch-path", "--diagnostic-dir", "--report-dir", "--cpu-prof-dir", "--heap-prof-dir",
    }
)
_NODE_NO_SCRIPT = frozenset(
    {"-e", "--eval", "-p", "--print", "-pe", "-c", "--check", "-i", "--interactive", "-"}
)
_NODE_BOOL_OPTS = frozenset(
    {
        "--enable-source-maps", "--no-warnings", "--no-deprecation", "--expose-gc",
        "--preserve-symlinks", "--trace-warnings", "--trace-deprecation", "--throw-deprecation",
        "--abort-on-uncaught-exception", "--use-strict", "--pending-deprecation",
        "--experimental-vm-modules", "--experimental-strip-types", "--watch",
    }
)
_BUN_VALUE_OPTS = frozenset(
    {
        "--cwd", "-c", "--config", "--env-file", "-r", "--preload", "--import", "-d", "--define",
        "--port", "--tsconfig-override", "--install", "--elide-lines", "-F", "--filter",
        "--shell", "--loader", "--main-fields", "--conditions",
    }
)
_BUN_BOOL_OPTS = frozenset(
    {
        "--bun", "--watch", "--hot", "--silent", "--smol", "--no-install", "--if-present",
        "--prefer-offline", "--prefer-latest", "--no-addons", "--no-clear-screen", "--verbose",
    }
)
_BUN_SUBCOMMANDS = frozenset(
    {
        "install", "i", "add", "a", "remove", "rm", "update", "test", "build", "create", "init",
        "link", "unlink", "pm", "upgrade", "repl", "outdated", "audit", "publish", "patch",
    }
)


def _node_script(args: list[str]) -> str | None:
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            return args[i + 1] if i + 1 < len(args) else None
        if tok in _NODE_NO_SCRIPT or tok.split("=", 1)[0] in _NODE_NO_SCRIPT:
            return None
        if not tok.startswith("-"):
            return tok
        name, eq, _ = tok.partition("=")
        if eq:
            if not name.startswith("-"):
                return None
            i += 1  # --opt=value
        elif tok in _NODE_VALUE_OPTS:
            i += 2
        elif tok in _NODE_BOOL_OPTS:
            i += 1
        else:
            return None  # unknown option: arity unknown, fail closed
    return None


def _bun_opts(args: list[str]) -> list[str] | None:
    """Drop leading options; return the rest, or None if an option is not understood."""
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            return args[i + 1 :]
        if not tok.startswith("-"):
            return args[i:]
        if "=" in tok:
            i += 1
        elif tok in _BUN_VALUE_OPTS:
            i += 2
        elif tok in _BUN_BOOL_OPTS:
            i += 1
        else:
            return None
    return []


def _bun_script(args: list[str], bunx: bool) -> str | None:
    """Script path or package token that would run, or None. Bare names never count as files."""
    rest = _bun_opts(args)
    if not rest:
        return None
    first = rest[0]
    if bunx or first in ("x", "bunx"):
        pkg_args = rest if bunx else _bun_opts(rest[1:])
        if not pkg_args:
            return None
        pkg = pkg_args[0]
        return pkg.rsplit("@", 1)[0] if "@" in pkg[1:] else pkg  # drop a version suffix
    if first == "run":
        rest = _bun_opts(rest[1:])
        if not rest:
            return None
        first = rest[0]
    elif first in _BUN_SUBCOMMANDS:
        return None
    return first if "/" in first else None  # a bare word is a package script, not a file


def _script_matches(script: str, name: str) -> bool:
    base = _base(script)
    return base == name or base in (name + ".js", name + ".mjs", name + ".cjs")


def runs_cli(argv: tuple[str, ...] | list[str], name: str) -> bool:
    """True if argv is the executable `name`, or node/bun running a script named `name`."""
    if not argv or not all(isinstance(a, str) for a in argv):
        return False
    exe = _base(argv[0])
    args = list(argv[1:])
    if exe in _NODE:
        script = _node_script(args)
        if script is not None and "/" not in script:
            script = None  # a bare word is ambiguous (relative name, option value): fail closed
    elif exe == "bun":
        script = _bun_script(args, bunx=False)
    elif exe == "bunx":
        script = _bun_script(args, bunx=True)
    else:
        return exe == name
    return script is not None and _script_matches(script, name)


def is_claude(argv: tuple[str, ...]) -> bool:
    return runs_cli(argv, "claude")


def is_codex(argv: tuple[str, ...]) -> bool:
    if argv and isinstance(argv[0], str) and _base(argv[0]) in _CODEX_HOSTS:
        return True
    return runs_cli(argv, "codex")


def default_lister() -> Iterable[Any]:
    return psutil.process_iter()


def default_cache(lister: Lister | None, cache: ProcCache | None) -> ProcCache | None:
    """The given cache; a new one for the real process table; none for an injected lister.

    An injected lister (tests) supplies its own handles and is read directly.
    """
    if cache is not None:
        return cache
    return ProcCache(psutil) if lister is None else None


def read_table(lister: Lister) -> dict[int, Entry]:
    """Snapshot the process table; processes that vanish or deny access are skipped."""
    table: dict[int, Entry] = {}
    for n, handle in enumerate(lister()):
        if n >= MAX_TABLE:
            break
        try:
            pid = int(handle.pid)
            ppid = int(handle.ppid())
            argv = tuple(str(a) for a in handle.cmdline())
            started = float(handle.create_time())
        except (psutil.Error, OSError, ValueError, TypeError):
            continue
        table[pid] = Entry(pid, ppid, argv, started, handle)
    return table


def owning_claude(table: dict[int, Entry], entry: Entry) -> int | None:
    """Pid of the nearest Claude CLI ancestor, or None.

    An ancestor that started after the child cannot be its parent (pid reuse),
    which also ends the walk. Cycles and long chains end it too.
    """
    seen = {entry.pid}
    child = entry
    for _ in range(MAX_DEPTH):
        parent = table.get(child.ppid)
        if parent is None or parent.pid in seen or parent.started > child.started:
            return None
        if is_claude(parent.argv):
            return parent.pid
        seen.add(parent.pid)
        child = parent
    return None


def collect_procs(
    lister: Lister,
    match: Callable[[tuple[str, ...]], bool],
    agent: str,
    show_args: set[str],
    cache: ProcCache | None = None,
    *,
    args_mode: str = "allowlist",
    value_flags: frozenset[str] = VALUE_FLAGS,
    here: dict[int, HereRecord] | None = None,
    now: float = 0.0,
) -> SourceResult:
    table = read_table(lister)
    rows: list[Proc] = []
    sessions: list[Session] = []
    seen: set[tuple[int, float]] = set()
    for pid in sorted(table):
        entry = table[pid]
        if not match(entry.argv):
            continue
        try:
            if cache is None:
                cpu = float(entry.handle.cpu_percent(None))
            else:
                seen.add((pid, entry.started))
                cpu = cache.cpu(pid, entry.started, proc=entry.handle)
            rss = int(entry.handle.memory_info().rss)
        except (psutil.Error, OSError, ValueError, TypeError):
            continue
        rec = here.get(pid) if here else None
        # PID reuse: the process now holding this pid must have started no later than the record
        # (it is a codex process, as the match above requires), else the file is someone else's.
        if rec is not None and entry.started <= rec.started_at + START_SLACK_S:
            sessions.append(
                Session(
                    pid=pid, agent=agent, session_id=rec.session_id,
                    project=Stamped("", now), project_rule="none", task=agent, state="unknown",
                    branch="", worktree="", started=entry.started, cpu=cpu, rss=rss,
                    name=f"{agent}-{rec.session_id[:_NAME_ID_CHARS]}", address=rec.session_id,
                )
            )
            if len(rows) + len(sessions) >= MAX_ROWS:
                break
            continue
        rows.append(
            Proc(
                pid=pid,
                ppid=entry.ppid,
                session_pid=owning_claude(table, entry),
                agent=sanitize_text(agent, 32),
                cmd=sanitize_text(
                    redact_argv(
                        list(entry.argv), show_args, value_flags=value_flags, mode=args_mode
                    ),
                    1000,
                ),
                cpu=cpu,
                rss=rss,
                started=entry.started,
            )
        )
        if len(rows) + len(sessions) >= MAX_ROWS:
            break
    if cache is not None:
        cache.end_cycle(seen)
    return SourceResult(procs=rows, sessions=sessions, status="ok")


class CodexSource:
    name = "codex"

    def __init__(
        self,
        lister: Lister | None = None,
        show_args: set[str] | None = None,
        *,
        cache: ProcCache | None = None,
        args_mode: str = "allowlist",
        value_flags: frozenset[str] | None = None,
        here_dir: Path | None = None,
    ) -> None:
        self._here_dir = here_dir
        self._args_mode = args_mode
        self._value_flags = VALUE_FLAGS if value_flags is None else frozenset(value_flags)
        self._cache = default_cache(lister, cache)
        self._lister: Lister = lister if lister is not None else default_lister
        self._show_args = set(show_args or ())

    def collect(self, ctx: Ctx) -> SourceResult:
        here = {r.pid: r for r in read_here_dir(self._here_dir)}
        return collect_procs(
            self._lister, is_codex, "codex", self._show_args, self._cache, args_mode=self._args_mode,
            value_flags=self._value_flags, here=here, now=ctx.clock(),
        )

