"""`fafo line`: a one-line summary for a status bar, built from one snapshot.

Pure: no I/O, no clock, no subprocess. Segments carry only fixed labels and counts (never
snapshot text), so nothing a hostile process or session name holds can reach the line. Each
segment still goes through sanitize_text, the same function the table uses, before the
command's own color codes are added.

Order: cl (Claude sessions busy/total), cx (Codex processes active/total), ol (Ollama busy
requests/loaded models), gm (Gemini processes active), rn (runners busy/total), cp (Copilot
cloud runs running). A segment shows only while something is busy, or while its source is down
(then it ends in `!`). Everything idle prints an empty line.
"""

from __future__ import annotations

import math

from ..model import Snapshot
from ..redact import sanitize_text

ACTIVE_CPU = 1.0  # percent: a Codex or Gemini process at or above this counts as active
MAX_FUTURE_S = 60.0  # a snapshot stamped further ahead than this is not trusted
MAX_SEGMENT = 24
NO_COLLECTOR_MARK = "fafo: no collector"
ELLIPSIS = "..."
SEP = " "
DIM, RED, RESET = "\x1b[2m", "\x1b[31m", "\x1b[0m"

_SOURCE = {"cl": "claude", "cx": "codex", "ol": "ollama", "gm": "gemini", "rn": "runners",
           "cp": "copilot"}


def _cpu_active(cpu: object) -> bool:
    return isinstance(cpu, (int, float)) and math.isfinite(cpu) and cpu >= ACTIVE_CPU


def _segments(snap: Snapshot) -> list[tuple[str, str]]:
    """(text, level) per visible segment, level one of ok, stale, down."""
    status = {h.name: h.status for h in snap.health}

    def level(key: str, down: bool = False) -> str:
        st = status.get(_SOURCE[key])
        return "down" if down or st == "down" else ("stale" if st == "stale" else "ok")

    claude = [s for s in snap.sessions if s.agent == "claude" and s.pid > 0]
    cl_busy = sum(1 for s in claude if s.state == "busy")
    codex = [p for p in snap.procs if p.agent == "codex"]
    cx_busy = sum(1 for p in codex if _cpu_active(p.cpu))
    ol_busy = sum(max(s.queue, len(s.inflight)) for s in snap.llms)
    ol_models = sum(len(s.models) for s in snap.llms)
    ol_down = any(not s.reachable for s in snap.llms)
    gm_busy = sum(1 for p in snap.procs if p.agent == "gemini" and _cpu_active(p.cpu))
    rn_busy = sum(1 for r in snap.runners if r.state == "busy")
    rn_down = any(r.state == "down" for r in snap.runners)
    cp_busy = sum(1 for s in snap.sessions if s.agent == "copilot" and s.state == "busy")

    rows = [
        ("cl", cl_busy, f"{cl_busy}/{len(claude)}", False),
        ("cx", cx_busy, f"{cx_busy}/{len(codex)}", False),
        ("ol", ol_busy, f"{ol_busy}/{ol_models}", ol_down),
        ("gm", gm_busy, f"{gm_busy}", False),
        ("rn", rn_busy, f"{rn_busy}/{len(snap.runners)}", rn_down),
        ("cp", cp_busy, f"{cp_busy}", False),
    ]
    out: list[tuple[str, str]] = []
    for key, busy, counts, down in rows:
        lvl = level(key, down)
        if busy <= 0 and lvl != "down":
            continue
        text = f"{key} {counts}" if busy > 0 else key
        out.append((sanitize_text(text + ("!" if lvl == "down" else ""), MAX_SEGMENT), lvl))
    return out


def _paint(text: str, lvl: str, color: bool) -> str:
    if not color or lvl == "ok":
        return text
    return f"{RED if lvl == 'down' else DIM}{text}{RESET}"


def _fit(segs: list[tuple[str, str]], width: int, color: bool) -> str:
    """Whole segments that fit in `width` visible characters; a cut ends with the ellipsis."""
    texts = [t for t, _ in segs]
    keep = len(segs)
    cut = len(SEP.join(texts)) > width
    if cut:
        while keep > 0 and len(SEP.join([*texts[:keep], ELLIPSIS])) > width:
            keep -= 1
    parts = [_paint(t, lvl, color) for t, lvl in segs[:keep]]
    if cut:
        if keep == 0 and width < len(ELLIPSIS):
            return ""
        parts.append(ELLIPSIS)
    return SEP.join(parts)


def render_line(
    snap: Snapshot | None,
    now: float,
    *,
    width: int,
    color: bool,
    stale_after: float,
    quiet: bool,
) -> str:
    """The status line without its newline: at most `width` visible characters."""
    age = now - snap.taken if snap is not None and math.isfinite(snap.taken) else None
    if snap is None or age is None or age > stale_after or age < -MAX_FUTURE_S:
        return "" if quiet else NO_COLLECTOR_MARK[: max(0, width)]
    return _fit(_segments(snap), width, color)
