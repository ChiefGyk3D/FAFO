"""Plain-text and JSON output over the shared view model. No colors, no control codes."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import asdict, dataclass

from ..jsonsafe import finite_copy
from ..model import Snapshot
from .view import (
    Group,
    ProcNode,
    SessionNode,
    Views,
    age_text,
    snapshot_status,
)

ELLIPSIS = "…"
MIN_WIDTH = 20
GAP = "  "
COL_CAP = 28
ELASTIC_CAP = 40
MIN_ELASTIC = 8


def clip(text: str, width: int) -> str:
    """Truncate to width characters, ending in an ellipsis when something was cut."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return ELLIPSIS
    return text[: width - 1] + ELLIPSIS


@dataclass
class _Row:
    cells: list[str]
    prefix: str = "  "
    indent: int = 0


_Item = _Row | str  # a str is a raw line (group header, "+N more")


def _grid(
    headers: list[str],
    items: list[_Item],
    priority: list[str],
    elastic: set[str],
    width: int,
) -> list[str]:
    """Render rows as aligned columns, dropping low-priority columns until the lines fit."""
    rows = [i for i in items if isinstance(i, _Row)]
    prefix_w = max((len(r.prefix) for r in rows), default=2)
    avail = width - prefix_w
    natural: dict[str, int] = {}
    for idx, h in enumerate(headers):
        longest = max([len(h)] + [len(r.cells[idx]) + (r.indent if idx == 0 else 0) for r in rows])
        cap = ELASTIC_CAP if h in elastic else COL_CAP
        natural[h] = min(longest, cap)

    chosen: dict[str, int] = {}
    used = 0
    for h in priority:
        need = natural[h] + (len(GAP) if chosen else 0)
        if used + need <= avail:
            chosen[h] = natural[h]
            used += need
        elif not chosen:
            chosen[h] = max(1, avail)
            used = avail
        elif h in elastic and avail - used - len(GAP) >= MIN_ELASTIC:
            w = avail - used - len(GAP)
            chosen[h] = w
            used += w + len(GAP)
    order = [h for h in headers if h in chosen]

    def fmt(cells: list[str], prefix: str, indent: int) -> str:
        parts = []
        for h in order:
            idx = headers.index(h)
            cell = cells[idx]
            if idx == 0 and indent:
                cell = " " * indent + cell
            parts.append(clip(cell, chosen[h]).ljust(chosen[h]))
        return clip((prefix + GAP.join(parts)).rstrip(), width)

    out = [fmt(headers, " " * prefix_w, 0)]
    for item in items:
        if isinstance(item, str):
            out.append(clip(item, width))
        else:
            out.append(fmt(item.cells, item.prefix.ljust(prefix_w), item.indent))
    return out


_S_HEAD = ["PID", "NAME", "PROJECT", "TASK", "STATE", "ELAPSED", "CPU", "AGENT", "BRANCH", "RSS"]
_S_PRIO = ["PROJECT", "STATE", "NAME", "PID", "TASK", "ELAPSED", "CPU", "AGENT", "BRANCH", "RSS"]
_R_HEAD = ["RUNNER", "STATE", "REPO", "WORKFLOW", "JOB", "STEP", "ELAPSED", "LOAD", "MEM", "DISK"]
_R_PRIO = ["RUNNER", "STATE", "JOB", "STEP", "ELAPSED", "LOAD", "REPO", "WORKFLOW", "MEM", "DISK"]


def _state(state: str, stale: bool, age: str) -> str:
    return f"{state} ~{age}" if stale and age else state


def _session_row(n: SessionNode, indent: int = 0) -> _Row:
    return _Row(
        [
            str(n.pid) if n.pid else "-",
            n.name or "-",
            n.project,
            n.task,
            _state(n.state, n.stale, n.age),
            n.elapsed,
            n.cpu,
            n.agent,
            n.branch,
            n.rss,
        ],
        prefix="* " if n.is_current else "  ",
        indent=indent,
    )


def _proc_row(p: ProcNode, indent: int) -> _Row:
    return _Row(
        [str(p.pid), "", "", p.cmd, _state("", p.stale, p.age).strip(), "", p.cpu, p.agent, "", p.rss],
        indent=indent,
    )


def _group_items(g: Group) -> list[_Item]:
    count = len(g.procs) + len(g.sessions) + g.hidden
    items: list[_Item] = [f"  {g.name} ({count})"]
    items += [_proc_row(p, 1) for p in g.procs]
    items += [_session_row(n, 1) for n in g.sessions]
    if g.hidden:
        items.append(f"  +{g.hidden} more")
    return items


def format_table(views: Views, width: int = 100) -> str:
    width = max(width, MIN_WIDTH)
    lines: list[str] = []

    h = views.host
    head = ["FAFO", f"view {age_text(views.age_s)}"]
    for label, val in (("load", h.load), ("cpu", h.cpu), ("mem", h.mem), ("gpu", h.gpu)):
        if val:
            head.append(f"{label} {val}")
    lines.append("  ".join(head))
    if views.health:
        strip = "  ".join(f"{s.name} {s.status} {s.age}" for s in views.health)
        lines.append(f"sources: {strip}")
    if views.message:
        lines.append(views.message)
    if views.banner is not None:
        lines.append(views.banner.text)

    lines.append("")
    lines.append("SESSIONS")
    if views.sessions:
        items: list[_Item] = []
        for n in views.sessions:
            items.append(_session_row(n))
            items += [_proc_row(c, 2) for c in n.children]
            if n.children_hidden:
                items.append(f"    +{n.children_hidden} more processes")
        if views.sessions_hidden:
            items.append(f"  +{views.sessions_hidden} more sessions")
        lines += _grid(_S_HEAD, items, _S_PRIO, {"TASK"}, width)
    else:
        lines.append("  none")

    if views.unattached:
        lines.append("")
        lines.append("UNATTACHED")
        items = []
        for g in views.unattached:
            items += _group_items(g)
        lines += _grid(_S_HEAD, items, _S_PRIO, {"TASK"}, width)

    stale_age = age_text(views.age_s)
    if views.llms:
        lines.append("")
        lines.append("LOCAL LLM")
        for llm in views.llms:
            mark = f" ~{llm.age}" if llm.stale else ""
            if llm.label:
                lines.append(f"  server {llm.name}{mark}")
            for m in llm.models:
                lines.append(f"  {m.name}  {m.vram}  ctx {m.context}  exp {m.expires}{mark}")
            if llm.models_hidden:
                lines.append(f"  +{llm.models_hidden} more models")
            if not llm.models:
                lines.append("  no model loaded")
            gpu = f"  gpu {llm.gpu}" if llm.gpu else ""
            lines.append(f"  queue {llm.queue}  in-flight {len(llm.inflight)}{gpu}")
            for i in llm.inflight:
                lines.append(f"  in-flight {i.model}  session {i.session}")
        if views.llms_hidden:
            lines.append(f"  +{views.llms_hidden} more servers")

    if views.runners:
        lines.append("")
        lines.append("RUNNERS")
        items = []
        for r in views.runners:
            items.append(
                _Row(
                    [
                        r.name, _state(r.state, r.stale, stale_age), r.repo, r.workflow,
                        r.job, r.step, r.elapsed, r.load, r.mem, r.disk,
                    ]
                )
            )
        if views.runners_hidden:
            items.append(f"  +{views.runners_hidden} more runners")
        lines += _grid(_R_HEAD, items, _R_PRIO, {"JOB", "REPO", "WORKFLOW"}, width)

    if views.quota:
        lines.append("")
        lines.append("QUOTA")
        for q in views.quota:
            lines.append(f"  {q.name}  {q.text}")

    return "\n".join(clip(line, width) for line in lines) + "\n"


def _dump(doc) -> str:
    return json.dumps(finite_copy(doc), indent=2, sort_keys=True, allow_nan=False) + "\n"


def _snapshot_doc(snap: Snapshot) -> dict:
    """Snapshot as plain dicts. asdict only sees the fixed-shape rows; the free-form host,
    quota and gpu values (untrusted nesting) are copied by the iterative finite_copy instead."""
    shell = dataclasses.replace(
        snap,
        host={},
        quota={},
        llms=[dataclasses.replace(s, gpu={}) for s in snap.llms],
    )
    doc = asdict(shell)
    doc["host"] = finite_copy(snap.host)
    doc["quota"] = finite_copy(snap.quota)
    for copy, server in zip(doc["llms"], snap.llms):
        copy["gpu"] = finite_copy(server.gpu)
    doc["llm"] = doc["llms"][0] if doc["llms"] else None  # first server, for older readers
    return doc


def format_json(snap: Snapshot | None, now: float) -> str:
    """Snapshot as JSON (sorted keys, indent 2) with status and age added at the top level."""
    status, age = snapshot_status(snap, now)
    if snap is None:
        return _dump({"status": status})
    doc = _snapshot_doc(snap)
    doc["status"] = status
    doc["age_s"] = age
    return _dump(doc)


def format_sessions(views: Views) -> str:
    """One line per Claude session: `name  session_id  project`, current session marked with `*`.

    Every line carries a two character lead ("* " or "  ") so columns line up.
    """
    nodes = [n for n in views.sessions if n.agent == "claude"]
    if not nodes:
        return ""
    name_w = max(len(n.name or "-") for n in nodes)
    id_w = max(len(n.session_id) for n in nodes)
    lines = []
    for n in nodes:
        lead = "* " if n.is_current else "  "
        lines.append(f"{lead}{(n.name or '-').ljust(name_w)}  {n.session_id.ljust(id_w)}  {n.project}")
    return "\n".join(line.rstrip() for line in lines) + "\n"
