"""Ready-made views.json for the Claude Code pane.

The pane is a TypeScript mod: it cannot run Python and must not spawn a subprocess per
refresh, so the collector writes this file next to the snapshot every tick and the pane
only reads and draws it (the mod runtime reads text files up to 4 MiB).

The file is the shared view model as JSON, schema version 2. The writer does not know which
session is looking at it, so `is_current` is ALWAYS false and `banner` is ALWAYS null here.
The pane learns its own session id from the mod API and marks the matching row itself.

The display cap in build_views hides sessions beyond MAX_SESSIONS, so `sessions` can lack the
pane's own session. Schema 2 adds `session_index`: a compact list of EVERY known Claude session
(not Copilot cloud rows), ordered by pid, at most 2000 entries. The pane finds its session in
session_index, then in sessions if present. The index is the last thing trimmed: size trimming
drops children, then whole sessions, and leaves the index alone.

All strings come out of build_views already sanitized; json.dumps with ensure_ascii escapes
anything that is left, so the encoded file never holds a raw control character.
"""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from ..model import Snapshot
from .view import _CAP_ID, _CAP_NAME, _CAP_PROJECT, _CAP_SHORT, Views, _s

SCHEMA_VERSION = 2
MAX_INDEX = 2000
MAX_BYTES = 2 * 1024 * 1024  # well under the 4 MiB the mod runtime can read
_TRIM_STEPS = (20, 0)  # children kept per session, tried in order when the file is too big


def _finite(v: float | None) -> float | None:
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None


def _index_entry(session_id, name, pid, project, state, address) -> dict:
    return {
        "session_id": _s(session_id, _CAP_ID),
        "name": _s(name, _CAP_NAME),
        "pid": pid,
        "project": _s(project, _CAP_PROJECT),
        "state": _s(state, _CAP_SHORT),
        "address": _s(address or session_id, _CAP_ID),
    }


def build_session_index(views: Views, snapshot: Snapshot | None) -> list[dict]:
    """Every known Claude session, by pid, capped at MAX_INDEX. Uses the snapshot when given
    (the view model hides sessions past its display cap); else the sessions the view shows."""
    if snapshot is not None:
        rows = [
            _index_entry(x.session_id, x.name, x.pid, x.project.value, x.state, x.address)
            for x in snapshot.sessions
            if x.agent == "claude" and x.pid != 0
        ]
    else:
        rows = [
            _index_entry(n.session_id, n.name, n.pid, n.project, n.state, n.address)
            for n in views.sessions
            if n.agent == "claude"
        ]
    rows.sort(key=lambda r: r["pid"])
    return rows[:MAX_INDEX]


def views_to_dict(
    views: Views, *, snapshot: Snapshot | None = None, generated: float | None = None
) -> dict:
    """JSON-safe dict of the view model. is_current is always False and banner is None."""
    doc = asdict(views)
    doc["banner"] = None
    doc["age_s"] = _finite(views.age_s)
    for node in doc["sessions"]:
        node["is_current"] = False
    for group in doc["unattached"]:
        for node in group["sessions"]:
            node["is_current"] = False
    gen = _finite(time.time() if generated is None else generated)
    return {
        "schema": SCHEMA_VERSION,
        "generated": gen,
        "session_index": build_session_index(views, snapshot),
        **doc,
    }


def _encode(doc: dict) -> bytes:
    return json.dumps(doc, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("ascii")


def _trim(doc: dict, keep: int) -> None:
    for node in doc["sessions"]:
        extra = len(node["children"]) - keep
        if extra > 0:
            node["children_hidden"] += extra
            del node["children"][keep:]


def encode_bounded(doc: dict) -> bytes:
    """Encode, trimming until under MAX_BYTES: children (20, then 0), then whole sessions
    (halving). session_index is never trimmed here; it is capped at MAX_INDEX when built."""
    payload = _encode(doc)
    for keep in _TRIM_STEPS:
        if len(payload) <= MAX_BYTES:
            return payload
        _trim(doc, keep)
        payload = _encode(doc)
    while len(payload) > MAX_BYTES and doc["sessions"]:
        cut = len(doc["sessions"]) // 2
        doc["sessions_hidden"] += len(doc["sessions"]) - cut
        del doc["sessions"][cut:]
        payload = _encode(doc)
    return payload


def write_views(
    path: Path, views: Views, *, snapshot: Snapshot | None = None, generated: float | None = None
) -> None:
    """Atomically write views.json (temp file, fsync, 0600, os.replace). Raises OSError only."""
    path = Path(path)
    try:
        payload = encode_bounded(views_to_dict(views, snapshot=snapshot, generated=generated))
    except (ValueError, TypeError, RecursionError) as exc:
        raise OSError(f"views not encodable: {type(exc).__name__}") from None
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
