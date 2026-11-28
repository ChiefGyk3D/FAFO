"""Codex source: process tree only.

Finds Codex processes (`codex`, `codex-code-mode-host`, node running a script
named `codex`) through an injectable lister and attaches each to the nearest
Claude Code ancestor. No Codex session or history file is read here.

The lister returns psutil.Process-like objects (pid, ppid(), cmdline(),
cpu_percent(), memory_info(), create_time()). Any of those can fail because the
process exited or is not ours: such a process is skipped, never fatal.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import psutil

from ..model import Proc
from ..proccache import ProcCache
from ..redact import redact_argv, sanitize_text
from ..source import Ctx, SourceResult

MAX_ROWS = 128
MAX_TABLE = 50000  # processes examined per pass
MAX_DEPTH = 64  # ancestor hops
VALUE_FLAGS = frozenset({"--model", "--resume"})
_CODEX_HOSTS = frozenset({"codex-code-mode-host"})

Lister = Callable[[], Iterable[Any]]


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
) -> SourceResult:
    table = read_table(lister)
    rows: list[Proc] = []
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
        if len(rows) >= MAX_ROWS:
            break
    if cache is not None:
        cache.end_cycle(seen)
    return SourceResult(procs=rows, status="ok")


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
    ) -> None:
        self._args_mode = args_mode
        self._value_flags = VALUE_FLAGS if value_flags is None else frozenset(value_flags)
        self._cache = default_cache(lister, cache)
        self._lister: Lister = lister if lister is not None else default_lister
        self._show_args = set(show_args or ())

    def collect(self, ctx: Ctx) -> SourceResult:
        return collect_procs(
            self._lister, is_codex, "codex", self._show_args, self._cache, args_mode=self._args_mode,
            value_flags=self._value_flags,
        )

