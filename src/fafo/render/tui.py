"""Full-screen observer (Textual). Reads ONLY the view model.

A background thread calls read_views every refresh interval and posts the result to
the UI; the UI thread never touches the filesystem. A failed read keeps the last good
views on screen, marked stale with their age. Every string is drawn through Rich Text
objects (never markup) after another pass of sanitize_text, so a process name such as
"[red]x[/]" or one carrying escape sequences shows literally and cannot restyle or
break the layout. Read-only: nothing is copied out, no network, no child processes.
"""

from __future__ import annotations

import argparse
import dataclasses
import gc
import logging
import math
import textwrap
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, NamedTuple

from rich.cells import cell_len, set_cell_size
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import Input, Static

from ..redact import sanitize_text
from .view import (
    Group,
    HelperPolicy,
    ProcNode,
    SessionNode,
    Views,
    age_text,
    helper_summary,
    read_views,
)

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())  # never write to the terminal the UI owns

# Dark only. Every fill is dark; every foreground is readable on it.
PALETTE = {
    "bg": "#0d1117",
    "panel_bg": "#161b22",
    "sel_bg": "#1a2f4d",
    "fg": "#c9d1d9",
    "busy": "#3fb950",
    "waiting": "#d29922",
    "idle": "#8b949e",
    "stale": "#6e7681",
    "down": "#f85149",
    "head": "#58a6ff",
    "cur": "#79c0ff",
}
THEME_NAME = "fafo-dark"

VIEW_NAMES = ("Sessions", "Processes", "Local LLM", "Runners", "Quota", "Session")
N_VIEWS = len(VIEW_NAMES)
V_SESSIONS, V_PROCS, V_LLM, V_RUNNERS, V_QUOTA, V_DRILL = range(N_VIEWS)

MIN_W = 60
MIN_H = 12
GAP = 2
MAX_STATUS = 160
MAX_EXPANDED = 256  # sessions whose helper rows are shown; the oldest toggle drops first
REFRESH_MIN = 0.25
REFRESH_MAX = 30.0

KEYS_LONG = (
    "q quit  Tab/S-Tab view  1-6 jump  j/k move  PgUp/PgDn page  g/G ends  "
    "/ filter  Enter open  Esc back  Space fold  x helpers  p pause"
)
KEYS_MID = (
    "q quit  Tab view  1-6 jump  j/k move  g/G ends  / filter  Enter open  Esc back  "
    "x helpers  p pause"
)
KEYS_SHORT = "q quit  Tab view  / filter  Enter open  Esc back  p pause"

TWISTY_OPEN = "▾"
TWISTY_SHUT = "▸"
MARK_HERE = "▶"
ELLIPSIS = "…"


# ---- small text helpers -----------------------------------------------------------------


def _one_line(text: object, cap: int = MAX_STATUS) -> str:
    s = sanitize_text(str(text), cap * 2)
    return " ".join(s.split())[:cap]


def fit(s: str, width: int, right: bool = False) -> str:
    """Exactly `width` terminal cells: clipped with an ellipsis or padded."""
    if width <= 0:
        return ""
    s = sanitize_text(s, max(width * 2, 8))
    if cell_len(s) > width:
        return set_cell_size(s, width - 1) + ELLIPSIS if width > 1 else ELLIPSIS
    pad = width - cell_len(s)
    return " " * pad + s if right else s + " " * pad


_style_cache: dict[tuple[str, str, bool], Style] = {}


def style_of(color: str, bg: str = "bg", bold: bool = False) -> Style:
    key = (color, bg, bold)
    st = _style_cache.get(key)
    if st is None:
        st = Style(color=PALETTE[color], bgcolor=PALETTE[bg], bold=bold)
        _style_cache[key] = st
    return st


def new_text() -> Text:
    return Text(no_wrap=True, overflow="crop")


def state_color(state: str) -> str:
    return {
        "busy": "busy", "running": "busy", "waiting": "waiting", "idle": "idle",
        "down": "down", "offline": "down", "error": "down", "failed": "down",
    }.get(state, "fg")


# ---- pages: flat, lazily rendered row lists -----------------------------------------------


class Item(NamedTuple):
    kind: str  # sess child gsess gproc head more proc model runner quota line
    key: tuple
    a: Any = None
    b: Any = None
    c: Any = None


Cell = tuple[str, str]  # (text, palette color)


@dataclass(frozen=True)
class Col:
    head: str
    width: int
    prio: int  # lower survives narrower terminals
    flex: bool = False
    right: bool = False


@dataclass
class Page:
    cols: list[Col]
    items: list[Item]
    cells: Callable[[Item], tuple[str, dict[str, Cell], bool]] | None
    empty: str = ""


def choose_cols(cols: list[Col], avail: int) -> tuple[list[Col], dict[str, int]]:
    """Keep columns by priority while they fit; the flex column absorbs what is left."""
    chosen: list[Col] = []
    used = 0
    for c in sorted(cols, key=lambda c: c.prio):
        need = c.width + (GAP if chosen else 0)
        if used + need <= avail:
            chosen.append(c)
            used += need
    if not chosen and cols:
        c = min(cols, key=lambda c: c.prio)
        return [c], {c.head: max(1, avail)}
    ordered = [c for c in cols if c in chosen]
    widths = {c.head: c.width for c in ordered}
    flex = next((c for c in ordered if c.flex), None)
    if flex is not None:
        widths[flex.head] += max(0, avail - used)
    return ordered, widths


def _matches(node: SessionNode, needle: str) -> bool:
    return not needle or any(
        needle in f.lower() for f in (node.agent, node.project, node.name, node.state)
    )


def _label(node: SessionNode) -> str:
    return node.name or node.project or f"pid {node.pid}"


def _session_cells(node: SessionNode, twisty: str, kids: int) -> dict[str, Cell]:
    dim = node.stale
    count = f" ({kids})" if kids else ""
    state = f"{node.state} ~{node.age}" if node.stale and node.age else node.state
    c_state = "stale" if dim else state_color(node.state)
    c_main = "stale" if dim else "fg"
    return {
        "NAME": (f"{twisty}{_label(node)}{count}", "cur" if node.is_current else c_main),
        "PID": (str(node.pid), c_main),
        "AGENT": (node.agent, c_main),
        "STATE": (state, c_state),
        "PROJECT": (node.project, c_main),
        "TASK": (node.task, c_main),
        "BRANCH": (node.branch, "stale" if dim else "idle"),
        "ELAPSED": (node.elapsed, c_main),
        "CPU": (node.cpu, c_main),
        "MEM": (node.rss, c_main),
    }


def _proc_cells(p: ProcNode, owner: str = "") -> dict[str, Cell]:
    c = "stale" if p.stale else "fg"
    return {
        "NAME": ("   └─", "stale"),
        "PID": (str(p.pid), c),
        "AGENT": (p.agent, c),
        "TASK": (p.cmd, c),
        "CMD": (p.cmd, c),
        "SESSION": (owner or "-", c),
        "CPU": (p.cpu, c),
        "MEM": (p.rss, c),
    }


SESSION_COLS = [
    Col("NAME", 24, 1),
    Col("PID", 8, 5),
    Col("AGENT", 8, 6),
    Col("STATE", 12, 2),
    Col("PROJECT", 18, 3),
    Col("TASK", 24, 4, flex=True),
    Col("BRANCH", 16, 10),
    Col("ELAPSED", 8, 7, right=True),
    Col("CPU", 5, 8, right=True),
    Col("MEM", 6, 9, right=True),
]
PROC_COLS = [
    Col("PID", 8, 1),
    Col("SESSION", 22, 3),
    Col("AGENT", 8, 4),
    Col("CMD", 24, 2, flex=True),
    Col("CPU", 5, 5, right=True),
    Col("MEM", 6, 6, right=True),
]
MODEL_COLS = [
    Col("MODEL", 24, 1, flex=True),
    Col("VRAM", 7, 2, right=True),
    Col("CONTEXT", 8, 3, right=True),
    Col("EXPIRES", 8, 4, right=True),
]
RUNNER_COLS = [
    Col("NAME", 14, 1),
    Col("STATE", 8, 2),
    Col("JOB", 20, 3, flex=True),
    Col("REPO", 22, 4),
    Col("WORKFLOW", 18, 5),
    Col("STEP", 6, 6, right=True),
    Col("ELAPSED", 9, 7, right=True),
    Col("LOAD", 5, 8, right=True),
    Col("MEM", 5, 9, right=True),
    Col("DISK", 5, 10, right=True),
]
QUOTA_COLS = [Col("NAME", 24, 1), Col("USAGE", 16, 2, flex=True)]


def _group_procs(g: Group, needle: str) -> list[ProcNode]:
    return [p for p in g.procs if not needle or needle in g.name.lower() or needle in p.agent.lower()]


def _helper_text(node: SessionNode, open_: bool) -> str:
    text = helper_summary(node.collapsed, node.collapsed_names)
    return f"{text} (shown, x hides)" if open_ else f"{text} (x shows)"


def build_sessions_page(
    views: Views, collapsed: set, needle: str, expanded: frozenset | set = frozenset()
) -> Page:
    items: list[Item] = []
    add = items.append
    for node in views.sessions:
        if not _matches(node, needle):
            continue
        sid = node.session_id
        open_ = sid not in collapsed
        add(Item("sess", ("s", sid), node, open_))
        if open_:
            for ch in node.children:
                add(Item("child", ("c", sid, ch.pid), ch, node))
            if node.children_hidden:
                add(Item("more", ("m", sid), f"+{node.children_hidden} more not shown", node))
            if node.collapsed:
                shown = sid in expanded
                add(Item("helpers", ("h", sid), _helper_text(node, shown), node))
                if shown:
                    for ch in node.helpers:
                        add(Item("child", ("c", sid, ch.pid), ch, node))
                    if node.helpers_hidden:
                        add(Item("more", ("mh", sid), f"+{node.helpers_hidden} more helpers not shown",
                                 node))
    if views.sessions_hidden and not needle:
        add(Item("more", ("m", "sessions"), f"+{views.sessions_hidden} more sessions not shown"))
    for g in views.unattached:
        procs = _group_procs(g, needle)
        sess_ = [s for s in g.sessions if _matches(s, needle)]
        if needle and not procs and not sess_:
            continue
        open_ = g.name not in collapsed
        add(Item("head", ("g", g.name), f"{g.name} ({len(procs) + len(sess_) + g.hidden})", open_))
        if open_:
            for p in procs:
                add(Item("gproc", ("gp", g.name, p.pid), p, g.name))
            for s in sess_:
                add(Item("gsess", ("gs", g.name, s.session_id, s.name), s))
            if g.hidden:
                add(Item("more", ("m", g.name), f"+{g.hidden} more not shown"))

    def cells(it: Item) -> tuple[str, dict[str, Cell], bool]:
        if it.kind == "sess":
            node = it.a
            kids = len(node.children) + node.children_hidden + node.collapsed
            tw = (TWISTY_OPEN if it.b else TWISTY_SHUT) + " "
            mark = MARK_HERE if node.is_current else " "
            return mark, _session_cells(node, tw, kids), node.is_current
        if it.kind == "gsess":
            return " ", _session_cells(it.a, "  ", 0), False
        return " ", _proc_cells(it.a), False

    return Page(SESSION_COLS, items, cells, "no sessions")


def build_procs_page(
    views: Views, needle: str, expanded: frozenset | set = frozenset()
) -> Page:
    items: list[Item] = []
    add = items.append
    for node in views.sessions:
        if not _matches(node, needle):
            continue
        add(Item("proc", ("p", node.pid, "session"), node, _label(node)))
        for ch in node.children:
            add(Item("proc", ("p", ch.pid, node.session_id), ch, _label(node)))
        if node.children_hidden:
            add(Item("more", ("m", node.session_id), f"+{node.children_hidden} more not shown"))
        if node.collapsed:
            shown = node.session_id in expanded
            add(Item("helpers", ("h", node.session_id), _helper_text(node, shown), node))
            if shown:
                for ch in node.helpers:
                    add(Item("proc", ("p", ch.pid, node.session_id), ch, _label(node)))
                if node.helpers_hidden:
                    add(Item("more", ("mh", node.session_id),
                             f"+{node.helpers_hidden} more helpers not shown", node))
    for g in views.unattached:
        for p in _group_procs(g, needle):
            add(Item("proc", ("p", p.pid, g.name), p, "-"))
        for s in g.sessions:
            if _matches(s, needle):
                add(Item("proc", ("p", s.pid, g.name, s.name), s, s.name))
        if g.hidden and not needle:
            add(Item("more", ("m", g.name), f"+{g.hidden} more not shown"))
    if views.sessions_hidden and not needle:
        add(Item("more", ("m", "sessions"), f"+{views.sessions_hidden} more sessions not shown"))

    def cells(it: Item) -> tuple[str, dict[str, Cell], bool]:
        a = it.a
        if isinstance(a, SessionNode):
            c = "stale" if a.stale else "fg"
            d = {
                "PID": (str(a.pid), c), "SESSION": (str(it.b), c), "AGENT": (a.agent, c),
                "CMD": (f"session: {a.task}", c), "CPU": (a.cpu, c), "MEM": (a.rss, c),
            }
            return " ", d, False
        return " ", _proc_cells(a, str(it.b)), False

    return Page(PROC_COLS, items, cells, "no processes")


def _wrap(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, width=max(10, width), break_long_words=True,
                         break_on_hyphens=False) or [""]


def build_llm_page(views: Views, needle: str) -> Page:
    llm = views.llm
    if llm is None:
        return Page([], [], None, "local LLM: not reported")
    items: list[Item] = []
    c = "stale" if llm.stale else "head"
    summary = f"queue {llm.queue}   in-flight {len(llm.inflight)}   gpu {llm.gpu or '?'}"
    items.append(Item("line", ("l", "summary"), summary, c))
    for m in llm.models:
        if needle and needle not in m.name.lower():
            continue
        items.append(Item("model", ("model", m.name), m, llm.stale))
    if llm.models_hidden:
        items.append(Item("more", ("m", "models"), f"+{llm.models_hidden} more models not shown"))
    for i, f in enumerate(llm.inflight):
        if needle and needle not in f.model.lower() and needle not in f.session.lower():
            continue
        items.append(Item("line", ("l", "inflight", i), f"  in-flight: {f.model}  <-  {f.session}",
                          "stale" if llm.stale else "fg"))

    def cells(it: Item) -> tuple[str, dict[str, Cell], bool]:
        m = it.a
        col = "stale" if it.b else "fg"
        return " ", {"MODEL": (m.name, col), "VRAM": (m.vram, col),
                     "CONTEXT": (m.context, col), "EXPIRES": (m.expires, col)}, False

    return Page(MODEL_COLS, items, cells, "no models loaded")


def build_runners_page(views: Views, needle: str) -> Page:
    items = [
        Item("runner", ("r", r.name, i), r)
        for i, r in enumerate(views.runners)
        if not needle
        or any(needle in f.lower() for f in (r.name, r.state, r.repo, r.workflow, r.job))
    ]
    if views.runners_hidden and not needle:
        items.append(Item("more", ("m", "runners"), f"+{views.runners_hidden} more runners not shown"))

    def cells(it: Item) -> tuple[str, dict[str, Cell], bool]:
        r = it.a
        col = "stale" if r.stale else "fg"
        return " ", {
            "NAME": (r.name, col), "STATE": (r.state, "stale" if r.stale else state_color(r.state)),
            "JOB": (r.job, col), "REPO": (r.repo, col), "WORKFLOW": (r.workflow, col),
            "STEP": (r.step, col), "ELAPSED": (r.elapsed, col), "LOAD": (r.load, col),
            "MEM": (r.mem, col), "DISK": (r.disk, col),
        }, False

    return Page(RUNNER_COLS, items, cells, "no runners reported")


def build_quota_page(views: Views, needle: str) -> Page:
    items = [Item("quota", ("q", q.name), q) for q in views.quota
             if not needle or needle in q.name.lower()]

    def cells(it: Item) -> tuple[str, dict[str, Cell], bool]:
        q = it.a
        return " ", {"NAME": (q.name, "fg"), "USAGE": (q.text, "fg")}, False

    return Page(QUOTA_COLS, items, cells, "no quota data")


def _find_session(views: Views, sid: str | None) -> SessionNode | None:
    if sid is None:
        return None
    for n in views.sessions:
        if n.session_id == sid:
            return n
    for g in views.unattached:
        for n in g.sessions:
            if n.session_id == sid:
                return n
    return None


def build_drill_page(
    views: Views, sid: str | None, width: int, expanded: frozenset | set = frozenset()
) -> Page:
    node = _find_session(views, sid)
    lines: list[tuple[str, str]] = []

    def proc_lines(rows: list[ProcNode]) -> None:
        for ch in rows:
            lines.append((f"  {ch.pid:>8}  {ch.agent:<8}  cpu {ch.cpu:>4}  mem {ch.rss:>5}",
                          "stale" if ch.stale else "fg"))
            for chunk in _wrap(ch.cmd, width - 6):
                lines.append((f"      {chunk}", "idle"))

    if node is None:
        lines.append(("no session selected, or it has ended. Esc goes back.", "waiting"))
    else:
        def kv(k: str, v: str, color: str = "fg") -> None:
            lines.append((f"{k:<13}{v}", color))

        lines.append((f"SESSION  {_label(node)}", "head"))
        kv("address", node.address, "cur")
        kv("session id", node.session_id)
        kv("name", node.name or "-")
        kv("project", node.project)
        kv("project_rule", node.project_rule)
        kv("task", node.task)
        kv("state", node.state + (f"  (stale {node.age})" if node.stale else ""),
           "stale" if node.stale else state_color(node.state))
        kv("agent", node.agent)
        kv("pid", str(node.pid))
        kv("worktree", node.worktree or "-")
        kv("branch", node.branch or "-")
        kv("elapsed", f"{node.elapsed}   cpu {node.cpu}   mem {node.rss}")
        lines.append(("", "fg"))
        total = len(node.children) + node.children_hidden + node.collapsed
        lines.append((f"children ({total})", "head"))
        proc_lines(node.children)
        if node.children_hidden:
            lines.append((f"  +{node.children_hidden} more not shown", "waiting"))
    items = [Item("line", ("l", i), t, c) for i, (t, c) in enumerate(lines)]
    if node is not None and node.collapsed:
        shown = node.session_id in expanded
        items.append(Item("helpers", ("h", node.session_id), _helper_text(node, shown), node))
        if shown:
            lines.clear()
            proc_lines(node.helpers)
            if node.helpers_hidden:
                lines.append((f"  +{node.helpers_hidden} more helpers not shown", "waiting"))
            n = len(items)
            items += [Item("line", ("l", n + i), t, c) for i, (t, c) in enumerate(lines)]
    return Page([], items, None, "")


# ---- messages / widgets --------------------------------------------------------------------


class ReadResult(Message):
    """One refresh outcome from the reader thread. views is None only when the read raised."""

    def __init__(
        self, views: Views | None, changed: bool, at: float, error: str, sig: Any = None
    ) -> None:
        super().__init__()
        self.sig = sig  # signature of views; the UI records it only when it APPLIES the result
        self.views = views
        self.changed = changed
        self.at = at
        self.error = error


class Body(Widget, can_focus=True):
    def render(self) -> Text:
        app: Any = self.app
        try:
            lines = app.body_lines(self.size.width, self.size.height)
        except Exception as e:  # never a traceback on screen
            log.exception("tui: render failed")
            t = new_text()
            t.append(_one_line(f"render error: {type(e).__name__}: {e}", 120), style_of("down"))
            return t
        return Text("\n", no_wrap=True, overflow="crop").join(lines)


@dataclass
class UiState:
    collapsed: set = field(default_factory=set)
    needle: str = ""
    # session ids whose helper rows are shown; insertion ordered, bounded by MAX_EXPANDED
    expanded: dict = field(default_factory=dict)

    def toggle_expanded(self, sid: str) -> None:
        if sid in self.expanded:
            del self.expanded[sid]
            return
        self.expanded[sid] = None
        while len(self.expanded) > MAX_EXPANDED:
            del self.expanded[next(iter(self.expanded))]


class FafoApp(App[int]):
    ENABLE_COMMAND_PALETTE = False
    AUTO_FOCUS = "#body"
    CSS = f"""
    Screen {{ background: {PALETTE['bg']}; color: {PALETTE['fg']}; }}
    #main {{ width: 100%; height: 100%; background: {PALETTE['bg']}; }}
    #banner, #header, #health, #status, #keys {{
        height: 1; width: 100%; background: {PALETTE['panel_bg']}; color: {PALETTE['fg']};
    }}
    #banner {{ display: none; }}
    #body {{ width: 100%; height: 1fr; background: {PALETTE['bg']}; }}
    #filter {{
        display: none; height: 1; width: 100%; border: none; padding: 0 1;
        background: {PALETTE['panel_bg']}; color: {PALETTE['fg']};
    }}
    #filter:focus {{ border: none; background: {PALETTE['sel_bg']}; }}
    #small {{
        display: none; width: 100%; height: 100%; background: {PALETTE['bg']};
        color: {PALETTE['waiting']};
    }}
    """
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit_app", "quit", show=False),
        Binding("tab", "next_view", "next view", show=False, priority=True),
        Binding("shift+tab", "prev_view", "previous view", show=False, priority=True),
        *[Binding(str(i), f"jump({i})", f"view {i}", show=False) for i in range(1, N_VIEWS + 1)],
        Binding("j,down", "move(1)", "down", show=False),
        Binding("k,up", "move(-1)", "up", show=False),
        Binding("pagedown", "page(1)", "page down", show=False),
        Binding("pageup", "page(-1)", "page up", show=False),
        Binding("g,home", "edge(0)", "top", show=False),
        Binding("G,shift+g,end", "edge(1)", "bottom", show=False),
        Binding("slash", "filter", "filter", show=False),
        Binding("enter", "open", "open", show=False),
        Binding("escape", "back", "back", show=False),
        Binding("space", "toggle", "fold", show=False),
        Binding("x", "helpers", "helpers", show=False),
        Binding("right", "expand(1)", "expand", show=False),
        Binding("left", "expand(0)", "collapse", show=False),
        Binding("p", "pause", "pause", show=False),
    ]

    def __init__(self, snapshot_path: Path, session_id: str | None = None,
                 refresh_s: float = 1.0, helpers: HelperPolicy | None = None) -> None:
        super().__init__()
        self.helpers = helpers
        self.snapshot_path = Path(snapshot_path)
        self.session_id = session_id
        self.refresh_s = refresh_s
        self.views: Views = Views(status="no-collector", age_s=None, message="reading snapshot...")
        self.last_good: Views | None = None
        self.last_good_at = 0.0
        self.read_note = ""
        self.fault = ""
        self.paused = False
        self.reads = 0
        self.state = UiState()
        self.view_idx = V_SESSIONS
        self.prev_view = V_SESSIONS
        self.drill_id: str | None = None
        self.page = Page([], [], None, "")
        self.sel_idx = 0
        self.sel_key: tuple | None = None
        self.top = 0
        self.mem: dict[int, tuple[tuple | None, int, int]] = {}
        self.small = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._unpaused = threading.Event()
        self._unpaused.set()
        self._thread: threading.Thread | None = None
        self._applied_sig: Any = None  # written by the UI thread only, read by the reader

    # -- layout --------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="main"):
            yield Static(Text(""), id="banner")
            yield Static(Text(""), id="header")
            yield Static(Text(""), id="health")
            yield Body(id="body")
            yield Input(id="filter", placeholder="filter: agent, project, name, state")
            yield Static(Text(""), id="status")
            yield Static(Text(""), id="keys")
        yield Static(Text(""), id="small")

    def on_mount(self) -> None:
        theme = Theme(
            name=THEME_NAME, primary=PALETTE["head"], secondary=PALETTE["cur"],
            warning=PALETTE["waiting"], error=PALETTE["down"], success=PALETTE["busy"],
            accent=PALETTE["head"], foreground=PALETTE["fg"], background=PALETTE["bg"],
            surface=PALETTE["panel_bg"], panel=PALETTE["panel_bg"], dark=True,
        )
        self.register_theme(theme)
        self.theme = THEME_NAME
        self._thread = threading.Thread(target=self._reader, name="fafo-reader", daemon=True)
        self._thread.start()
        self._check_size()
        self._chrome()

    def on_unmount(self) -> None:
        self._stop.set()
        self._unpaused.set()
        self._wake.set()
        t = self._thread
        if t is not None:
            t.join(timeout=0.5)

    def on_resize(self, event: object) -> None:
        self._safe_call(self._after_resize)

    def _after_resize(self) -> None:
        self._check_size()
        if self.view_idx == V_DRILL:
            self._rebuild()
        self._chrome()
        self.query_one("#body", Body).refresh()

    def _check_size(self) -> None:
        w, h = self.size
        self.small = w < MIN_W or h < MIN_H
        self.query_one("#main").display = not self.small
        small = self.query_one("#small", Static)
        small.display = self.small
        if self.small:
            small.update(self._small_text())

    def _small_text(self) -> Text:
        t = new_text()
        w, h = self.size
        t.append(f"terminal too small ({w}x{h}); need {MIN_W}x{MIN_H}", style_of("waiting"))
        return t

    # -- reader thread (the only code that touches the filesystem) -----------------------------

    def _reader(self) -> None:
        while not self._stop.is_set():
            if self._unpaused.is_set():
                now = time.time()
                views: Views | None = None
                error = ""
                changed = False
                sig = None
                try:
                    views = read_views(
                        self.snapshot_path, now, current_session_id=self.session_id,
                        helpers=self.helpers,
                    )
                    if views.age_s is None:
                        error = "snapshot missing or unreadable"
                    else:
                        sig = dataclasses.replace(views, age_s=None)
                        # compared with what the UI has APPLIED, never with what was read
                        changed = sig != self._applied_sig
                except Exception as e:
                    log.exception("tui: caught, continuing")
                    views = None
                    error = _one_line(f"{type(e).__name__}: {e}", 100)
                try:
                    self.post_message(ReadResult(views, changed, now, error, sig))
                except Exception:
                    log.exception("tui: caught, continuing")
                    return
            self._wake.wait(self.refresh_s)
            self._wake.clear()

    # -- result handling -----------------------------------------------------------------------

    def on_read_result(self, msg: ReadResult) -> None:
        if self.paused:
            return
        self.handle_result(msg)

    def handle_result(self, res: ReadResult) -> None:
        try:
            self._handle(res)
        except Exception as e:
            log.exception("tui: caught, continuing")
            self.fault = _one_line(f"refresh error: {type(e).__name__}: {e}")
            self._safe_call(self._chrome)
        finally:
            self.reads += 1

    def _handle(self, res: ReadResult) -> None:
        good = res.views is not None and res.views.age_s is not None
        if good:
            assert res.views is not None
            first = self.last_good is None
            self.last_good = res.views
            self.last_good_at = res.at
            self.read_note = ""
            self.views = res.views
            if res.changed or first:
                self._rebuild()
                self.fault = ""  # cleared only once a rebuild really worked
            self._applied_sig = res.sig  # advance only after the result was applied
        elif self.last_good is not None:
            # keep showing the last good views; the header marks them stale with their age
            self.read_note = f"read failed: {res.error or 'unknown'}; showing last good data"
        else:
            if res.views is not None:
                self.views = res.views
            self.read_note = ""
            self._rebuild()
        self._chrome()

    # -- page building / selection ---------------------------------------------------------------

    def _build_page(self) -> Page:
        v = self.views
        needle = self.state.needle
        i = self.view_idx
        if i == V_SESSIONS:
            return build_sessions_page(v, self.state.collapsed, needle, self.state.expanded)
        if i == V_PROCS:
            return build_procs_page(v, needle, self.state.expanded)
        if i == V_LLM:
            return build_llm_page(v, needle)
        if i == V_RUNNERS:
            return build_runners_page(v, needle)
        if i == V_QUOTA:
            return build_quota_page(v, needle)
        return build_drill_page(
            v, self.drill_id, max(20, self.size.width - 2), self.state.expanded
        )

    def _rebuild(self) -> None:
        # Building tens of thousands of row tuples would trigger full collections over the
        # whole view tree; pause the collector for this one bounded pass.
        was_enabled = gc.isenabled()
        gc.disable()
        try:
            page = self._build_page()
        finally:
            if was_enabled:
                gc.enable()
        items = page.items
        idx = None
        if self.sel_key is not None:
            for n, it in enumerate(items):
                if it.key == self.sel_key:
                    idx = n
                    break
        if idx is None:
            idx = min(self.sel_idx, len(items) - 1) if items else 0
        self.page = page
        self.sel_idx = max(0, idx)
        self.sel_key = items[self.sel_idx].key if items else None
        self._scroll_into_view()
        self._paint()

    @property
    def selected_key(self) -> tuple | None:
        return self.sel_key

    def _visible(self) -> int:
        try:
            h = self.query_one("#body", Body).size.height
        except Exception:
            log.exception("tui: caught, continuing")
            h = 10
        return max(1, h - (1 if self.page.cols else 0))

    def _scroll_into_view(self) -> None:
        vis = self._visible()
        n = len(self.page.items)
        if self.sel_idx < self.top:
            self.top = self.sel_idx
        elif self.sel_idx >= self.top + vis:
            self.top = self.sel_idx - vis + 1
        self.top = max(0, min(self.top, max(0, n - vis)))

    def _select(self, idx: int) -> None:
        n = len(self.page.items)
        if n == 0:
            return
        self.sel_idx = max(0, min(n - 1, idx))
        self.sel_key = self.page.items[self.sel_idx].key
        self._scroll_into_view()
        self._paint()

    def _paint(self) -> None:
        try:
            self.query_one("#body", Body).refresh()
        except Exception:
            log.exception("tui: caught, continuing")
        self._chrome()

    # -- drawing -----------------------------------------------------------------------------

    def body_lines(self, width: int, height: int) -> list[Text]:
        out: list[Text] = []
        page = self.page
        vis = height - (1 if page.cols else 0)
        if height <= 0 or width <= 0:
            return out
        cols, widths = choose_cols(page.cols, max(1, width - 2)) if page.cols else ([], {})
        if page.cols:
            head = new_text()
            hs = style_of("head", "panel_bg", True)
            head.append(fit(" ", 2), hs)
            for n, c in enumerate(cols):
                if n:
                    head.append(" " * GAP, hs)
                head.append(fit(c.head, widths[c.head], c.right), hs)
            head.append(fit("", max(0, width - head.cell_len)), hs)
            out.append(head)
        items = page.items
        if not items:
            msg = self.views.message if self.views.message and not self.last_good else ""
            t = new_text()
            t.append(fit(msg or page.empty, width), style_of("waiting"))
            out.append(t)
        for n in range(self.top, min(len(items), self.top + max(0, vis))):
            out.append(self._row(items[n], n == self.sel_idx, cols, widths, width))
        blank = Text(" " * width, no_wrap=True, overflow="crop", style=style_of("fg"))
        while len(out) < height:
            out.append(blank)
        return out[:height]

    def _row(self, it: Item, selected: bool, cols: list[Col], widths: dict[str, int],
             width: int) -> Text:
        bg = "sel_bg" if selected else "bg"
        t = new_text()
        kind = it.kind
        if kind in ("head", "more", "line", "helpers"):
            if kind == "head":
                color, prefix = "head", (TWISTY_OPEN if it.b else TWISTY_SHUT) + " "
            elif kind == "helpers":
                color, prefix = "idle", "     └─ "
            elif kind == "more":
                color, prefix = "waiting", "  "
            else:
                color, prefix = str(it.b), "  "
            t.append(prefix + fit(str(it.a), max(0, width - 2)), style_of(color, bg, kind == "head"))
            return t
        cells_fn = self.page.cells
        if cells_fn is None:
            return t
        gutter, cells, current = cells_fn(it)
        t.append(fit(gutter, 1) + " ", style_of("cur", bg, True))
        used = 2
        for n, c in enumerate(cols):
            if n:
                t.append(" " * GAP, style_of("fg", bg))
                used += GAP
            text, color = cells.get(c.head, ("", "fg"))
            bold = current and c.head == "NAME"
            t.append(fit(text, widths[c.head], c.right), style_of(color, bg, bold))
            used += widths[c.head]
        t.append(" " * max(0, width - used), style_of("fg", bg))
        return t

    def _health_text(self, width: int) -> Text:
        v = self.views
        t = new_text()
        st = style_of("fg", "panel_bg")
        t.append("sources: ", st)
        if not v.health:
            t.append("none reported", style_of("idle", "panel_bg"))
        for h in v.health:
            col = {"ok": "busy", "stale": "waiting", "down": "down"}.get(h.status, "idle")
            t.append(f"{sanitize_text(h.name, 32)} ", st)
            t.append(f"{sanitize_text(h.status, 16)} {h.age}", style_of(col, "panel_bg"))
            t.append("  ", st)
        host = v.host
        bits = [f"{k} {x}" for k, x in (("load", host.load), ("cpu", host.cpu),
                                         ("mem", host.mem), ("gpu", host.gpu)) if x]
        if bits:
            t.append("| host " + "  ".join(bits), style_of("idle", "panel_bg"))
        return self._pad(t, width, "panel_bg")

    @staticmethod
    def _pad(t: Text, width: int, bg: str) -> Text:
        if t.cell_len > width:
            t.truncate(width, overflow="crop")
        else:
            t.append(" " * (width - t.cell_len), style_of("fg", bg))
        return t

    def _stale_age(self) -> float | None:
        v = self.views
        age = None
        if v.age_s is not None and v.status != "ok":
            age = v.age_s
        if self.read_note and self.last_good is not None:
            base = self.last_good.age_s or 0.0
            age = max(age or 0.0, base + max(0.0, time.time() - self.last_good_at))
        return age

    def _header_text(self, width: int) -> Text:
        n = len(self.page.items)
        t = new_text()
        t.append(f"FAFO  view {self.view_idx + 1}/{N_VIEWS} {VIEW_NAMES[self.view_idx]}",
                 style_of("fg", "panel_bg", True))
        a = self.sel_idx + 1 if n else 0
        t.append(f"   row {a} of {n}", style_of("fg", "panel_bg"))
        if self.paused:
            t.append("   PAUSED", style_of("waiting", "panel_bg", True))
        if self.state.needle:
            t.append(f"   filter: {_one_line(self.state.needle, 40)}", style_of("head", "panel_bg"))
        age = self._stale_age()
        if age is not None:
            t.append(f"   STALE {age_text(age)}", style_of("waiting", "panel_bg", True))
        if self.last_good is None and self.views.age_s is None:
            t.append(f"   {_one_line(self.views.message or 'collector not running', 60)}",
                     style_of("down", "panel_bg", True))
        return self._pad(t, width, "panel_bg")

    def _banner_text(self, width: int) -> Text:
        b = self.views.banner
        t = new_text()
        if b is not None:
            color = "waiting" if b.session is None else "cur"
            t.append(_one_line(b.text, 300), style_of(color, "panel_bg", True))
        return self._pad(t, width, "panel_bg")

    def _status_text(self, width: int) -> Text:
        t = new_text()
        msg = self.fault or self.read_note or (
            self.views.message if self.views.status != "ok" else "")
        color = "down" if self.fault else "waiting"
        t.append(_one_line(msg, min(MAX_STATUS, width)), style_of(color, "panel_bg"))
        return self._pad(t, width, "panel_bg")

    def _keys_text(self, width: int) -> Text:
        t = new_text()
        cand = KEYS_SHORT
        for c in (KEYS_LONG, KEYS_MID, KEYS_SHORT):
            cand = c
            if len(c) <= width:
                break
        t.append(cand, style_of("idle", "panel_bg"))
        return self._pad(t, width, "panel_bg")

    def _chrome(self) -> None:
        if self.small:
            self.query_one("#small", Static).update(self._small_text())
            return
        w = max(1, self.size.width)
        banner = self.query_one("#banner", Static)
        banner.display = self.views.banner is not None
        banner.update(self._banner_text(w))
        self.query_one("#header", Static).update(self._header_text(w))
        self.query_one("#health", Static).update(self._health_text(w))
        self.query_one("#status", Static).update(self._status_text(w))
        self.query_one("#keys", Static).update(self._keys_text(w))

    def dump(self) -> str:
        """Plain text of what is on screen (for tests and bug reports)."""
        if self.small:
            return self._small_text().plain
        w = self.size.width
        body = self.query_one("#body", Body)
        parts: list[str] = []
        if self.views.banner is not None:
            parts.append(self._banner_text(w).plain)
        parts.append(self._header_text(w).plain)
        parts.append(self._health_text(w).plain)
        parts += [t.plain for t in self.body_lines(body.size.width, body.size.height)]
        flt = self.query_one("#filter", Input)
        if flt.display:
            parts.append("/" + flt.value)
        parts.append(self._status_text(w).plain)
        parts.append(self._keys_text(w).plain)
        return "\n".join(p.rstrip() for p in parts)

    # -- actions -------------------------------------------------------------------------------

    def _safe_call(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as e:
            log.exception("tui: caught, continuing")
            self.fault = _one_line(f"error: {type(e).__name__}: {e}")
            try:
                self._chrome()
            except Exception:
                log.exception("tui: caught, continuing")

    def _switch(self, idx: int) -> None:
        idx %= N_VIEWS
        if idx == self.view_idx:
            return
        self.mem[self.view_idx] = (self.sel_key, self.sel_idx, self.top)
        if idx == V_DRILL and self.drill_id is None:
            b = self.views.banner
            if b is not None and b.session is not None:
                self.drill_id = b.session.session_id
            elif self.views.sessions:
                self.drill_id = self.views.sessions[0].session_id
        self.view_idx = idx
        self.sel_key, self.sel_idx, self.top = self.mem.get(idx, (None, 0, 0))
        self._rebuild()

    def action_quit_app(self) -> None:
        self._stop.set()
        self._wake.set()
        self.exit(0)

    def action_next_view(self) -> None:
        self._safe_call(lambda: self._switch(self.view_idx + 1))

    def action_prev_view(self) -> None:
        self._safe_call(lambda: self._switch(self.view_idx - 1))

    def action_jump(self, n: int) -> None:
        self._safe_call(lambda: self._switch(n - 1))

    def action_move(self, d: int) -> None:
        self._safe_call(lambda: self._select(self.sel_idx + d))

    def action_page(self, d: int) -> None:
        self._safe_call(lambda: self._select(self.sel_idx + d * max(1, self._visible() - 1)))

    def action_edge(self, end: int) -> None:
        self._safe_call(lambda: self._select(len(self.page.items) - 1 if end else 0))

    def action_filter(self) -> None:
        def go() -> None:
            inp = self.query_one("#filter", Input)
            inp.value = self.state.needle
            inp.display = True
            inp.focus()
            self._paint()
        self._safe_call(go)

    def _set_needle(self, text: str) -> None:
        self.state.needle = _one_line(text, 60).lower()
        self.sel_key = None
        self.sel_idx = 0
        self.top = 0
        self._rebuild()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._safe_call(lambda: self._set_needle(event.value))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._safe_call(self._close_filter)

    def _close_filter(self) -> None:
        self.query_one("#filter", Input).display = False
        self.query_one("#body", Body).focus()
        self._paint()

    def action_back(self) -> None:
        def go() -> None:
            inp = self.query_one("#filter", Input)
            if inp.display or self.state.needle:
                inp.value = ""
                inp.display = False
                self.query_one("#body", Body).focus()
                self._set_needle("")
            elif self.view_idx == V_DRILL:
                self._switch(self.prev_view)
        self._safe_call(go)

    def _selected_session_id(self) -> str | None:
        if not self.page.items:
            return None
        it = self.page.items[self.sel_idx]
        if it.kind in ("sess", "gsess"):
            return it.a.session_id
        if it.kind == "helpers":
            return it.b.session_id
        if it.kind == "child":
            return it.b.session_id
        if it.kind == "proc" and isinstance(it.a, SessionNode):
            return it.a.session_id
        if it.kind == "proc":
            for node in self.views.sessions:
                if it.key[2] == node.session_id:
                    return node.session_id
        return None

    def action_open(self) -> None:
        def go() -> None:
            if not self.page.items:
                return
            it = self.page.items[self.sel_idx]
            if self.view_idx == V_DRILL:
                if it.kind == "helpers":
                    self._toggle_helpers(it.b.session_id)
                return
            if it.kind == "head":
                self._toggle_key(it.key[1], None)
                return
            if it.kind == "helpers":
                self._toggle_helpers(it.b.session_id)
                return
            sid = self._selected_session_id()
            if sid is None:
                return
            self.drill_id = sid
            self.prev_view = self.view_idx
            self._switch(V_DRILL)
        self._safe_call(go)

    def _toggle_key(self, name: str, want_open: bool | None) -> None:
        col = self.state.collapsed
        if want_open is None:
            want_open = name in col
        if want_open:
            col.discard(name)
        else:
            col.add(name)
        self._rebuild()

    def _toggle_helpers(self, sid: str) -> None:
        self.state.toggle_expanded(sid)
        self._rebuild()

    def action_helpers(self) -> None:
        def go() -> None:
            if self.view_idx not in (V_SESSIONS, V_PROCS, V_DRILL) or not self.page.items:
                return
            it = self.page.items[self.sel_idx]
            node = None
            if self.view_idx == V_DRILL:
                node = _find_session(self.views, self.drill_id)
            elif it.kind == "sess":
                node = it.a
            elif it.kind in ("child", "more", "helpers") and isinstance(it.b, SessionNode):
                node = it.b
            elif it.kind == "proc" and isinstance(it.a, SessionNode):
                node = it.a
            elif it.kind == "proc":
                node = next((n for n in self.views.sessions if n.session_id == it.key[2]), None)
            elif it.kind == "more":
                node = next((n for n in self.views.sessions if n.session_id == it.key[1]), None)
            if node is not None and node.collapsed:
                self._toggle_helpers(node.session_id)
        self._safe_call(go)

    def action_toggle(self) -> None:
        self._safe_call(lambda: self._fold(None))

    def action_expand(self, want: int) -> None:
        self._safe_call(lambda: self._fold(bool(want)))

    def _fold(self, want_open: bool | None) -> None:
        if self.view_idx != V_SESSIONS or not self.page.items:
            return
        it = self.page.items[self.sel_idx]
        if it.kind == "sess":
            self._toggle_key(it.a.session_id, want_open)
        elif it.kind == "head":
            self._toggle_key(it.key[1], want_open)
        elif it.kind in ("child", "more") and want_open is False and isinstance(it.b, SessionNode):
            self.sel_key = ("s", it.b.session_id)
            self._rebuild()

    def action_pause(self) -> None:
        def go() -> None:
            self.paused = not self.paused
            if self.paused:
                self._unpaused.clear()
            else:
                self._unpaused.set()
                self._wake.set()
            self._chrome()
        self._safe_call(go)


# ---- entry points --------------------------------------------------------------------------------


def run_tui(
    snapshot_path: Path,
    session_id: str | None = None,
    refresh_s: float = 1.0,
    helpers: HelperPolicy | None = None,
) -> int:
    app = FafoApp(Path(snapshot_path), session_id=session_id, refresh_s=refresh_s, helpers=helpers)
    code = app.run()
    return int(code) if isinstance(code, int) else 0


def _refresh_arg(text: str) -> float:
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {_one_line(text, 20)!r}") from None
    if not math.isfinite(v) or not REFRESH_MIN <= v <= REFRESH_MAX:
        raise argparse.ArgumentTypeError(f"must be between {REFRESH_MIN} and {REFRESH_MAX}")
    return v


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="fafo-tui", description="Full-screen FAFO observer.")
    p.add_argument("--snapshot", type=Path, default=None, help="snapshot file (default: config)")
    p.add_argument("--session", default=None, metavar="ID", help="mark this session id as 'you'")
    p.add_argument("--refresh", type=_refresh_arg, default=1.0, metavar="SECONDS",
                   help=f"refresh interval, {REFRESH_MIN} to {REFRESH_MAX} (default 1)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = args.snapshot
    if path is None:
        from ..config import load_config

        path = load_config(None).snapshot_path
    return run_tui(path, args.session, args.refresh)


if __name__ == "__main__":
    raise SystemExit(main())
