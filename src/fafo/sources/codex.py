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
from ..redact import redact_argv, sanitize_text
from ..source import Ctx, SourceResult

MAX_ROWS = 128
MAX_TABLE = 50000  # processes examined per pass
MAX_DEPTH = 64  # ancestor hops
VALUE_FLAGS = frozenset({"--model", "--resume"})
_RUNNERS = frozenset({"node", "nodejs", "bun"})
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


def runs_cli(argv: tuple[str, ...] | list[str], name: str) -> bool:
    """True if argv is the executable `name`, or node/bun running a script named `name`."""
    if not argv or not isinstance(argv[0], str):
        return False
    exe = _base(argv[0])
    if exe == name:
        return True
    if exe in _RUNNERS:
        for tok in argv[1:]:
            if not isinstance(tok, str) or tok.startswith("-"):
                continue
            return _base(tok) == name
    return False


def is_claude(argv: tuple[str, ...]) -> bool:
    return runs_cli(argv, "claude")


def is_codex(argv: tuple[str, ...]) -> bool:
    if argv and isinstance(argv[0], str) and _base(argv[0]) in _CODEX_HOSTS:
        return True
    return runs_cli(argv, "codex")


def default_lister() -> Iterable[Any]:
    return psutil.process_iter()


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
) -> SourceResult:
    table = read_table(lister)
    rows: list[Proc] = []
    for pid in sorted(table):
        entry = table[pid]
        if not match(entry.argv):
            continue
        try:
            cpu = float(entry.handle.cpu_percent(None))
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
                    redact_argv(list(entry.argv), show_args, value_flags=VALUE_FLAGS), 1000
                ),
                cpu=cpu,
                rss=rss,
                started=entry.started,
            )
        )
        if len(rows) >= MAX_ROWS:
            break
    return SourceResult(procs=rows, status="ok")


class CodexSource:
    name = "codex"

    def __init__(self, lister: Lister | None = None, show_args: set[str] | None = None) -> None:
        self._lister: Lister = lister if lister is not None else default_lister
        self._show_args = set(show_args or ())

    def collect(self, ctx: Ctx) -> SourceResult:
        return collect_procs(self._lister, is_codex, "codex", self._show_args)

