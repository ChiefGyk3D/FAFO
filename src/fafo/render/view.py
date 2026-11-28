"""Shared view model for every renderer (table, JSON, TUI, Claude pane).

Pure: build_views does no I/O and emits no terminal control codes. Every string
that came from a snapshot passes through sanitize_text and a length cap here, so
renderers can print the fields as they are. read_views is the one function that
touches the filesystem, through store.read_snapshot.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..config import DEFAULT_BORING_PROCS as DEFAULT_BORING
from ..model import DEFAULT_LLM_NAME, MAX_LLM_SERVERS, LlmServer, Proc, Session, Snapshot
from ..redact import sanitize_text
from ..store import read_snapshot

MAX_SESSIONS = 200
MAX_CHILDREN = 256  # rows kept per session for children and for expanded helpers; the rest are counted
MAX_GROUP_ROWS = 256
MAX_RUNNERS = 64
MAX_MODELS = 32
MAX_QUOTA = 32

HOT_CPU = 5.0  # percent: a boring process at or above this stays visible
MAX_HELPER_NAMES = 3

UNKNOWN = "?"
NO_COLLECTOR_TEXT = "collector not running"
UNKNOWN_SESSION_TEXT = "unknown session"
CLOUD_GROUP = "copilot cloud"

_STATE_RANK = {"busy": 0, "waiting": 1, "idle": 2}
_GROUP_ORDER = ("codex", "gemini", "other")

# Per-field caps (characters), applied after sanitizing.
_CAP_NAME = 64
_CAP_ID = 128
_CAP_PROJECT = 64
_CAP_TASK = 120
_CAP_CMD = 200
_CAP_SHORT = 64
_CAP_PATH = 128


def _s(value: object, cap: int) -> str:
    return sanitize_text(value if isinstance(value, str) else str(value), cap)


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def age_text(seconds: float | None) -> str:
    """Compact age: 6s, 2m, 3h, 2d. None is unknown."""
    if seconds is None or not math.isfinite(seconds):
        return UNKNOWN
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def elapsed_text(seconds: float | None) -> str:
    """Run time: 42s, 5m10s, 1h12m. None or non-positive input is unknown."""
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return UNKNOWN
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def size_text(n: float | None) -> str:
    if n is None or not math.isfinite(n) or n < 0:
        return UNKNOWN
    units = ("B", "K", "M", "G", "T")
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.0f}{units[i]}" if v >= 10 or i == 0 else f"{v:.1f}{units[i]}"


def pct_text(v: object) -> str:
    n = _num(v)
    if n is None or n < 0:
        return UNKNOWN
    return f"{n:.0f}%"


def _metric(v: float, fmt: str) -> str:
    """Runner metrics: negative (unknown, -1.0) shows as ?."""
    n = _num(v)
    if n is None or n < 0:
        return UNKNOWN
    return fmt.format(n)


@dataclass
class SourceView:
    name: str
    status: str
    age: str


@dataclass
class HostView:
    load: str = ""
    cpu: str = ""
    mem: str = ""
    gpu: str = ""


@dataclass
class ProcNode:
    pid: int
    agent: str
    cmd: str
    cpu: str
    rss: str
    stale: bool = False
    age: str = ""


@dataclass(frozen=True)
class HelperPolicy:
    """Which of a session's processes collapse into one summary row."""

    collapse: bool = True
    boring: frozenset[str] = frozenset(DEFAULT_BORING)


def policy_from_config(cfg: object) -> HelperPolicy:
    """The policy for a loaded Config (its two helper options)."""
    return HelperPolicy(
        collapse=bool(getattr(cfg, "collapse_helpers", True)),
        boring=frozenset(getattr(cfg, "boring_procs", DEFAULT_BORING)),
    )


def helper_summary(count: int, names: list[str]) -> str:
    """`+16 helpers (sleep x6, bash x4, cat x3)`; count is the exact number collapsed."""
    return f"+{count} helpers ({', '.join(names)})"


@dataclass
class SessionNode:
    name: str
    session_id: str
    address: str
    pid: int
    agent: str
    project: str
    project_rule: str
    task: str
    state: str
    branch: str
    worktree: str
    elapsed: str
    cpu: str
    rss: str
    is_current: bool = False
    stale: bool = False
    age: str = ""
    children: list[ProcNode] = field(default_factory=list)
    children_hidden: int = 0
    # Boring helper processes folded into one summary row (see HelperPolicy). `collapsed` is
    # the exact count; `helpers` keeps the rows for the TUI expand (capped, never exported).
    collapsed: int = 0
    collapsed_names: list[str] = field(default_factory=list)
    helpers: list[ProcNode] = field(default_factory=list)
    helpers_hidden: int = 0
    project_source: str = "live"


@dataclass
class Group:
    name: str
    procs: list[ProcNode] = field(default_factory=list)
    sessions: list[SessionNode] = field(default_factory=list)
    hidden: int = 0


@dataclass
class ModelView:
    name: str
    vram: str
    context: str
    expires: str


@dataclass
class InflightView:
    model: str
    session: str


@dataclass
class LlmView:
    models: list[ModelView] = field(default_factory=list)
    models_hidden: int = 0
    queue: int = 0
    inflight: list[InflightView] = field(default_factory=list)
    gpu: str = ""
    stale: bool = False
    name: str = DEFAULT_LLM_NAME
    label: bool = False  # show the server name: several servers, or one with a custom name
    age: str = ""  # age of this server's last good poll (the snapshot's age when unknown)


@dataclass
class RunnerView:
    name: str
    state: str
    repo: str
    workflow: str
    job: str
    step: str
    elapsed: str
    load: str
    mem: str
    disk: str
    stale: bool = False


@dataclass
class QuotaLine:
    name: str
    text: str


@dataclass
class Banner:
    text: str
    session: SessionNode | None = None


@dataclass
class Views:
    status: str  # "ok" | "stale" | "no-collector"
    age_s: float | None
    message: str = ""
    health: list[SourceView] = field(default_factory=list)
    host: HostView = field(default_factory=HostView)
    sessions: list[SessionNode] = field(default_factory=list)
    sessions_hidden: int = 0
    unattached: list[Group] = field(default_factory=list)
    llm: LlmView | None = None  # the first server, kept for older readers
    llms: list[LlmView] = field(default_factory=list)
    llms_hidden: int = 0
    runners: list[RunnerView] = field(default_factory=list)
    runners_hidden: int = 0
    quota: list[QuotaLine] = field(default_factory=list)
    banner: Banner | None = None


def _snapshot_status(
    snap: Snapshot | None, now: float, stale_after_s: float, dead_after_s: float
) -> tuple[str, float | None]:
    if snap is None:
        return "no-collector", None
    age = now - snap.taken
    if age > dead_after_s:
        return "no-collector", age
    if age > stale_after_s:
        return "stale", age
    return "ok", age


def snapshot_status(
    snap: Snapshot | None,
    now: float,
    stale_after_s: float = 15.0,
    dead_after_s: float = 60.0,
) -> tuple[str, float | None]:
    """Public: ("ok" | "stale" | "no-collector", age in seconds or None)."""
    return _snapshot_status(snap, now, stale_after_s, dead_after_s)


def _host_view(host: dict) -> HostView:
    v = HostView()
    load = _num(host.get("load1"))
    if load is not None:
        v.load = f"{load:.1f}"
    cpu = _num(host.get("cpu_percent"))
    if cpu is not None:
        v.cpu = f"{cpu:.0f}%"
    mem = _num(host.get("mem_percent"))
    if mem is not None:
        v.mem = f"{mem:.0f}%"
    gpus = host.get("gpus")
    if isinstance(gpus, list):
        utils = [u for g in gpus if isinstance(g, dict) and (u := _num(g.get("util"))) is not None]
        if utils:
            v.gpu = f"{max(utils):.0f}%"
    return v


def _proc_node(p: Proc, all_stale: bool, snap_age: str) -> ProcNode:
    return ProcNode(
        pid=p.pid,
        agent=_s(p.agent, _CAP_SHORT),
        cmd=_s(p.cmd, _CAP_CMD),
        cpu=pct_text(p.cpu),
        rss=size_text(p.rss),
        stale=all_stale,
        age=snap_age if all_stale else "",
    )


def project_label(value: str, source: str, cap: int = _CAP_PROJECT) -> str:
    """Project name for display, at most cap characters; a trailing `~` marks a last-known repo
    of an idle session (the name is cut to cap - 1 first so the marker always fits)."""
    if source == "last":
        return _s(value, cap - 1) + "~"
    return _s(value, cap)


def _session_node(
    s: Session, now: float, stale_after_s: float, all_stale: bool, snap_age: str
) -> SessionNode:
    field_age = now - s.project.as_of
    stale = all_stale or field_age > stale_after_s
    started = s.started if s.started > 0 else None
    return SessionNode(
        name=_s(s.name, _CAP_NAME),
        session_id=_s(s.session_id, _CAP_ID),
        address=_s(s.address or s.session_id, _CAP_ID),
        pid=s.pid,
        agent=_s(s.agent, _CAP_SHORT),
        project=project_label(s.project.value, s.project_source),
        project_rule=_s(s.project_rule, _CAP_SHORT),
        project_source=_s(s.project_source, _CAP_SHORT),
        task=_s(s.task, _CAP_TASK),
        state=_s(s.state, _CAP_SHORT),
        branch=_s(s.branch, _CAP_SHORT),
        worktree=_s(s.worktree, _CAP_PATH),
        elapsed=elapsed_text(now - started if started is not None else None),
        cpu=pct_text(s.cpu),
        rss=size_text(s.rss),
        stale=stale,
        age=(age_text(field_age) if field_age > stale_after_s else snap_age) if stale else "",
    )


def _exe(cmd: str) -> str:
    """Basename of the first word of a command line ("" for an empty one)."""
    words = cmd.split(None, 1)
    return words[0].rsplit("/", 1)[-1] if words else ""


def _split_helpers(procs: list[Proc], policy: HelperPolicy) -> tuple[list[Proc], list[Proc]]:
    """(visible, collapsed). A process collapses only when its program is boring, it is under
    HOT_CPU with a known cpu figure, and nothing visible hangs below it (ancestors of a
    visible process stay visible, so a boring wrapper never hides the real work under it)."""
    if not policy.collapse:
        return procs, []
    by_pid = {p.pid: p for p in procs}
    keep: set[int] = set()
    for p in procs:
        cpu = _num(p.cpu)
        if _exe(p.cmd) not in policy.boring or cpu is None or cpu >= HOT_CPU:
            keep.add(p.pid)
    for pid in list(keep):
        cur = by_pid[pid].ppid
        for _ in range(len(procs)):  # bounded walk: a ppid cycle cannot loop forever
            parent = by_pid.get(cur)
            if parent is None or parent.pid in keep:
                break
            keep.add(parent.pid)
            cur = parent.ppid
    return [p for p in procs if p.pid in keep], [p for p in procs if p.pid not in keep]


def _sort_key(n: SessionNode) -> tuple:
    return (
        not n.is_current,
        _STATE_RANK.get(n.state, 3),
        n.project.lower(),
        n.name.lower(),
        n.pid,
    )


def _left_text(seconds: float) -> str:
    n = int(seconds)
    if n >= 2 * 86400:
        return f"{n // 86400}d"
    if n >= 3600:
        return f"{n // 3600}h{(n % 3600) // 60:02d}m"
    return f"{max(n, 0) // 60}m"


def _window_text(w: dict, now: float) -> str:
    """One quota window {used_pct, resets_in_s, as_of, stale}: "41% resets 2h10m stale 2h"."""
    used = _num(w.get("used_pct"))
    text = f"{used:.0f}%" if used is not None else UNKNOWN
    left = _num(w.get("resets_in_s"))
    if left is not None and left > 0:
        text += f" resets {_left_text(left)}"
    if w.get("stale") is True:
        as_of = _num(w.get("as_of"))
        text += f" stale {age_text(now - as_of if as_of is not None else None)}"
    return text


def _quota_lines(quota: dict, now: float = 0.0) -> list[QuotaLine]:
    def fmt(v: object) -> str:
        n = _num(v)
        if n is not None:
            return f"{n:g}"
        if isinstance(v, dict) and "used_pct" in v:
            return _window_text(v, now)
        return _s(v, 40)

    out: list[QuotaLine] = []
    for key in sorted(quota, key=str)[:MAX_QUOTA]:
        val = quota[key]
        if isinstance(val, dict):
            parts = [f"{_s(k, 16)} {fmt(val[k])}" for k in sorted(val, key=str)[:8]]
            text = "  ".join(parts)
        else:
            text = fmt(val)
        out.append(QuotaLine(_s(key, 32), text))
    return out


def _llm_view(
    srv: LlmServer,
    names: dict[int, str],
    now: float,
    all_stale: bool,
    stale_after_s: float,
    snap_age: str,
    label: bool,
) -> LlmView:
    gpu = srv.gpu if isinstance(srv.gpu, dict) else {}
    # One server's rows go stale on their own: its last poll failed or is older than the limit.
    aged = srv.last_ok > 0 and now - srv.last_ok > stale_after_s
    stale = all_stale or not srv.reachable or aged
    if all_stale or srv.last_ok <= 0:
        age = UNKNOWN if (not all_stale and not srv.reachable) else snap_age
    else:
        age = age_text(now - srv.last_ok)
    return LlmView(
        models=[
            ModelView(
                name=_s(m.name, _CAP_NAME),
                vram=size_text(m.vram),
                context=f"{m.context // 1000}k" if m.context >= 1000 else str(m.context),
                expires=(
                    UNKNOWN
                    if m.expires <= 0
                    else ("expired" if m.expires <= now else age_text(m.expires - now))
                ),
            )
            for m in srv.models[:MAX_MODELS]
        ],
        models_hidden=max(0, len(srv.models) - MAX_MODELS),
        queue=srv.queue,
        inflight=[
            InflightView(
                model=_s(i.model, _CAP_NAME),
                session=_s(names.get(i.session_pid, UNKNOWN), _CAP_NAME)
                if i.session_pid is not None
                else UNKNOWN,
            )
            for i in srv.inflight[:MAX_MODELS]
        ],
        gpu=pct_text(gpu.get("util")) if _num(gpu.get("util")) is not None else "",
        stale=stale,
        name=_s(srv.name, _CAP_NAME),
        label=label,
        age=age,
    )


def build_views(
    snap: Snapshot | None,
    now: float,
    *,
    current_session_id: str | None = None,
    stale_after_s: float = 15.0,
    dead_after_s: float = 60.0,
    helpers: HelperPolicy | None = None,
) -> Views:
    policy = helpers if helpers is not None else HelperPolicy()
    status, age = _snapshot_status(snap, now, stale_after_s, dead_after_s)
    if snap is None:
        return Views(
            status="no-collector",
            age_s=None,
            message=NO_COLLECTOR_TEXT,
            banner=_banner(None, current_session_id),
        )

    snap_age = age_text(age)
    all_stale = status != "ok"
    message = ""
    if status == "no-collector":
        message = f"{NO_COLLECTOR_TEXT} (last update {snap_age} ago)"
    elif status == "stale":
        message = f"stale: last update {snap_age} ago"

    health = [
        SourceView(
            name=_s(h.name, _CAP_SHORT),
            status=_s(h.status, 16),
            age=age_text(now - h.last_ok) if h.last_ok > 0 else UNKNOWN,
        )
        for h in snap.health
    ]

    # Sessions: cloud runs (pid 0, copilot) are their own group, not Claude sessions.
    nodes: list[tuple[Session, SessionNode]] = []
    cloud: list[SessionNode] = []
    for s in snap.sessions:
        node = _session_node(s, now, stale_after_s, all_stale, snap_age)
        if s.pid == 0 and s.agent == "copilot":
            cloud.append(node)
        else:
            nodes.append((s, node))

    current: SessionNode | None = None
    if current_session_id is not None:
        for s, node in nodes:
            if current_session_id in (s.session_id, s.address):
                node.is_current = True
                current = node
                break
    session_pids = {s.pid for s, _ in nodes}

    owned: dict[int, list[Proc]] = {}
    orphans: list[Proc] = []
    for p in snap.procs:
        if p.session_pid is not None and p.session_pid in session_pids:
            owned.setdefault(p.session_pid, []).append(p)
        else:
            orphans.append(p)

    ordered = sorted((n for _, n in nodes), key=_sort_key)
    shown = ordered[:MAX_SESSIONS]
    for node in shown:
        procs = sorted(owned.get(node.pid, []), key=lambda p: p.pid)
        visible, folded = _split_helpers(procs, policy)
        node.children = [_proc_node(p, all_stale, snap_age) for p in visible[:MAX_CHILDREN]]
        node.children_hidden = max(0, len(visible) - MAX_CHILDREN)
        if folded:
            top = sorted(Counter(_exe(p.cmd) for p in folded).items(), key=lambda kv: (-kv[1], kv[0]))
            node.collapsed = len(folded)
            node.collapsed_names = [f"{n} x{c}" for n, c in top[:MAX_HELPER_NAMES]]
            node.helpers = [_proc_node(p, all_stale, snap_age) for p in folded[:MAX_CHILDREN]]
            node.helpers_hidden = max(0, len(folded) - MAX_CHILDREN)

    groups: dict[str, Group] = {}
    for p in sorted(orphans, key=lambda p: p.pid):
        gname = p.agent if p.agent in ("codex", "gemini") else "other"
        g = groups.setdefault(gname, Group(gname))
        if len(g.procs) < MAX_GROUP_ROWS:
            g.procs.append(_proc_node(p, all_stale, snap_age))
        else:
            g.hidden += 1
    unattached = [groups[n] for n in _GROUP_ORDER if n in groups]
    if cloud:
        cloud.sort(key=lambda n: (n.project.lower(), n.name.lower()))
        hidden = max(0, len(cloud) - MAX_GROUP_ROWS)
        unattached.append(Group(CLOUD_GROUP, sessions=cloud[:MAX_GROUP_ROWS], hidden=hidden))

    names = {s.pid: n.name or n.project or f"pid {s.pid}" for s, n in nodes}
    shown_llms = snap.llms[:MAX_LLM_SERVERS]
    label = len(shown_llms) > 1 or any(s.name != DEFAULT_LLM_NAME for s in shown_llms)
    llms = [
        _llm_view(s, names, now, all_stale, stale_after_s, snap_age, label) for s in shown_llms
    ]

    runners = [
        RunnerView(
            name=_s(r.name, _CAP_NAME),
            state=_s(r.state, 16),
            repo=_s(r.repo, _CAP_PROJECT),
            workflow=_s(r.workflow, _CAP_PROJECT),
            job=_s(r.job, _CAP_PROJECT),
            step=(
                f"{r.step_n}/{r.step_total}"
                if r.step_total > 0
                else ("" if r.state == "idle" else UNKNOWN)
            ),
            elapsed=elapsed_text(r.elapsed) if r.state == "busy" else "",
            load=_metric(r.load, "{:.1f}"),
            mem=_metric(r.mem, "{:.0f}%"),
            disk=_metric(r.disk, "{:.0f}%"),
            stale=all_stale,
        )
        for r in snap.runners[:MAX_RUNNERS]
    ]

    host = _host_view(snap.host)
    return Views(
        status=status,
        age_s=age,
        message=message,
        health=health,
        host=host,
        sessions=shown,
        sessions_hidden=max(0, len(ordered) - MAX_SESSIONS),
        unattached=unattached,
        llm=llms[0] if llms else None,
        llms=llms,
        llms_hidden=max(0, len(snap.llms) - len(llms)),
        runners=runners,
        runners_hidden=max(0, len(snap.runners) - MAX_RUNNERS),
        quota=_quota_lines(snap.quota, now),
        banner=_banner(current, current_session_id),
    )


def _banner(current: SessionNode | None, current_session_id: str | None) -> Banner | None:
    if current_session_id is None:
        return None
    if current is None:
        return Banner(UNKNOWN_SESSION_TEXT)
    text = (
        f"you are here: {current.name or '-'} | {current.project} | {current.task} "
        f"| pid {current.pid} | {current.state}"
    )
    return Banner(text, current)


def read_views(
    path: Path,
    now: float,
    *,
    current_session_id: str | None = None,
    stale_after_s: float = 15.0,
    dead_after_s: float = 60.0,
    helpers: HelperPolicy | None = None,
) -> Views:
    """The only function here that touches the filesystem."""
    return build_views(
        read_snapshot(Path(path)),
        now,
        current_session_id=current_session_id,
        stale_after_s=stale_after_s,
        dead_after_s=dead_after_s,
        helpers=helpers,
    )
