"""Render commands: `fafo table`, `fafo json`, `fafo sessions`, `fafo tui`.

All four only read the snapshot file. A missing or unreadable snapshot is not an error: the
view model shows "collector not running" and the exit code is 0. Printed text is re-stripped of
control characters here even though the view model already strips it (defense in depth).

`--session auto` finds the current Claude or Codex session by process ancestry: from this
process up through its parents, the nearest ancestor whose pid is the pid of a Claude or Codex
session in the snapshot (a Codex session exists only with the hook installed, see the README). It only works when the command runs from a terminal inside that session.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..config import load_config
from ..model import Session
from ..redact import sanitize_text
from ..store import read_snapshot
from .table import format_json, format_sessions, format_table
from .view import HelperPolicy, build_views, policy_from_config

EXIT_OK, EXIT_USAGE = 0, 2
MAX_HOPS = 64
MIN_WIDTH, MAX_WIDTH = 20, 300
DEFAULT_MIN_WIDTH = 40
FALLBACK_COLUMNS = 100
LINE_MIN_WIDTH, LINE_MAX_WIDTH, LINE_DEFAULT_WIDTH = 3, 300, 80
LINE_STALE_AFTER = 30.0
REFRESH_MIN, REFRESH_MAX = 0.25, 30.0
SESSION_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
TUI_HINT = "fafo: the tui needs Textual: pip install textual (or reinstall fafo with its dependencies)"

AUTO_HELP = (
    "session id to mark as 'you', or 'auto' to find it by process ancestry. auto only works "
    "when the command is run from a terminal inside a Claude or Codex session; otherwise no session "
    "is marked"
)

# Test seams.
_now: Callable[[], float] = time.time
_getpid: Callable[[], int] = os.getpid


def _real_parent_of(pid: int) -> int | None:
    """Parent pid, or None when the process is gone, unreadable or has no parent."""
    try:
        import psutil

        ppid = psutil.Process(pid).ppid()
    except Exception:  # noqa: BLE001 - vanished, denied or zombie: the walk just ends
        return None
    return ppid if isinstance(ppid, int) and ppid > 0 else None


_parent_of: Callable[[int], int | None] = _real_parent_of


# ---- argument types ------------------------------------------------------------------------


def session_arg(text: str) -> str:
    if text == "auto" or SESSION_ID_RE.fullmatch(text):
        return text
    raise argparse.ArgumentTypeError("invalid session id (allowed: A-Z a-z 0-9 . _ : -, 1-128)")


def width_arg(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("width must be a whole number") from None
    if not MIN_WIDTH <= n <= MAX_WIDTH:
        raise argparse.ArgumentTypeError(f"width must be between {MIN_WIDTH} and {MAX_WIDTH}")
    return n


def refresh_arg(text: str) -> float:
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("refresh must be a number") from None
    if not math.isfinite(v) or not REFRESH_MIN <= v <= REFRESH_MAX:
        raise argparse.ArgumentTypeError(f"refresh must be between {REFRESH_MIN} and {REFRESH_MAX}")
    return v


def line_width_arg(text: str) -> int:
    """`fafo line` never fails on its options: a bad width falls back, an extreme one is clamped."""
    try:
        n = int(text)
    except ValueError:
        return LINE_DEFAULT_WIDTH
    return max(LINE_MIN_WIDTH, min(LINE_MAX_WIDTH, n))


def stale_after_arg(text: str) -> float:
    try:
        v = float(text)
    except ValueError:
        return LINE_STALE_AFTER
    if not math.isfinite(v):
        return LINE_STALE_AFTER
    return max(1.0, min(86400.0, v))


def default_width() -> int:
    cols = shutil.get_terminal_size((FALLBACK_COLUMNS, 24)).columns
    return max(DEFAULT_MIN_WIDTH, min(MAX_WIDTH, cols))


# ---- current session by ancestry -----------------------------------------------------------


def find_current_session(
    sessions: Sequence[Session], start_pid: int, parent_of: Callable[[int], int | None]
) -> str | None:
    """session_id of the NEAREST ancestor (start_pid itself first) that is a Claude or Codex session.

    At most MAX_HOPS processes are examined; a repeated pid ends the walk (cycle); a lookup
    that fails or returns None ends it (vanished process).
    """
    by_pid = {s.pid: s.session_id for s in sessions if s.agent in ("claude", "codex") and s.pid > 0}
    seen: set[int] = set()
    pid: int | None = start_pid
    for _ in range(MAX_HOPS):
        if pid is None or pid <= 0 or pid in seen:
            return None
        if pid in by_pid:
            return by_pid[pid]
        seen.add(pid)
        try:
            pid = parent_of(pid)
        except Exception:  # noqa: BLE001 - tolerate any lookup failure
            return None
    return None


def _current_id(requested: str | None, snap: Any) -> str | None:
    if requested != "auto":
        return requested
    if snap is None:
        return None
    return find_current_session(snap.sessions, _getpid(), _parent_of)


# ---- handlers ------------------------------------------------------------------------------


def _snapshot_path(args: argparse.Namespace) -> Path:
    cfg = load_config(args.config)  # a bad config raises ConfigError: the caller exits 2
    if args.snapshot is not None:
        return args.snapshot.expanduser()
    return cfg.snapshot_path


def _helper_policy(args: argparse.Namespace) -> HelperPolicy:
    return policy_from_config(load_config(args.config))


def _clean(text: str) -> str:
    """Strip control characters and escape sequences from every line of output text."""
    lines = text.split("\n")
    return "\n".join(sanitize_text(line, len(line)) for line in lines)


def _emit(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _cmd_table(args: argparse.Namespace) -> int:
    snap = read_snapshot(_snapshot_path(args))
    sid = _current_id(args.session, snap)
    views = build_views(snap, _now(), current_session_id=sid, helpers=_helper_policy(args))
    width = args.width if args.width is not None else default_width()
    _emit(_clean(format_table(views, width)))
    return EXIT_OK


def _cmd_json(args: argparse.Namespace) -> int:
    snap = read_snapshot(_snapshot_path(args))
    _emit(format_json(snap, _now()))
    return EXIT_OK


def _cmd_sessions(args: argparse.Namespace) -> int:
    snap = read_snapshot(_snapshot_path(args))
    sid = _current_id(args.session, snap)
    views = build_views(snap, _now(), current_session_id=sid)
    if views.status == "no-collector":
        print("fafo: collector not running", file=sys.stderr)
    _emit(_clean(format_sessions(views)))
    return EXIT_OK


def _cmd_line(args: argparse.Namespace) -> int:
    """One line for a status bar. Always exit 0, never a traceback: a status line must not break
    the host UI. Any failure prints the no-collector marker (or nothing with --quiet)."""
    from . import line as linemod

    try:
        text = linemod.render_line(
            read_snapshot(_snapshot_path(args)), _now(), width=args.width, color=args.color,
            stale_after=args.stale_after, quiet=args.quiet,
        )
    except Exception:  # noqa: BLE001 - a status line never fails: marker, not a traceback
        text = "" if args.quiet else linemod.NO_COLLECTOR_MARK[: args.width]
    try:
        _emit(text + "\n")
    except OSError:  # closed pipe: the reader went away
        pass
    return EXIT_OK


def _cmd_tui(args: argparse.Namespace) -> int:
    path = _snapshot_path(args)
    try:
        from .tui import run_tui  # lazy: only this command needs Textual
    except ImportError:
        print(TUI_HINT, file=sys.stderr)
        return EXIT_USAGE
    sid = _current_id(args.session, read_snapshot(path) if args.session == "auto" else None)
    return run_tui(path, sid, args.refresh, _helper_policy(args))


# ---- registration --------------------------------------------------------------------------


def _common(p: argparse.ArgumentParser, session: bool = True) -> None:
    p.add_argument("--config", type=Path, default=None, help="TOML config file")
    p.add_argument(
        "--snapshot", type=Path, default=None,
        help="snapshot file to read (default: snapshot_path from the config)",
    )
    if session:
        p.add_argument("--session", type=session_arg, default=None, metavar="ID|auto", help=AUTO_HELP)


def add_render_commands(subparsers: Any) -> None:
    t = subparsers.add_parser("table", help="print the plain-text table once")
    _common(t)
    t.add_argument(
        "--width", type=width_arg, default=None, metavar="N",
        help=f"line width {MIN_WIDTH} to {MAX_WIDTH} (default: terminal width, {DEFAULT_MIN_WIDTH} to {MAX_WIDTH})",
    )
    t.set_defaults(handler=_cmd_table)

    j = subparsers.add_parser("json", help="print the snapshot as JSON")
    _common(j, session=False)
    j.set_defaults(handler=_cmd_json)

    s = subparsers.add_parser(
        "sessions", help="one line per Claude session: name, session id, project"
    )
    _common(s)
    s.set_defaults(handler=_cmd_sessions)

    ln = subparsers.add_parser(
        "line", help="one short status-line summary (empty when idle); always exits 0"
    )
    _common(ln, session=False)
    ln.add_argument(
        "--width", type=line_width_arg, default=LINE_DEFAULT_WIDTH, metavar="N",
        help=f"maximum visible width, clamped to {LINE_MIN_WIDTH} to {LINE_MAX_WIDTH} "
        f"(default {LINE_DEFAULT_WIDTH}); whole segments only, a cut ends in ...",
    )
    ln.add_argument("--color", action="store_true", help="dim stale and red down sources (default off)")
    ln.add_argument("--quiet", action="store_true", help="print nothing instead of the no-collector marker")
    ln.add_argument(
        "--stale-after", type=stale_after_arg, default=LINE_STALE_AFTER, metavar="SECONDS",
        help=f"snapshot older than this counts as no collector (default {LINE_STALE_AFTER:g})",
    )
    ln.set_defaults(handler=_cmd_line)

    u = subparsers.add_parser("tui", help="full-screen view (needs Textual)")
    _common(u)
    u.add_argument(
        "--refresh", type=refresh_arg, default=1.0, metavar="SECONDS",
        help=f"refresh interval, {REFRESH_MIN} to {REFRESH_MAX} (default 1)",
    )
    u.set_defaults(handler=_cmd_tui)
