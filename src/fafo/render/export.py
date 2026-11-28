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
session_index, then in sessions if present. Three more top-level keys let the pane tell a
missing session from a hidden one: `session_index_count` (the true number of Claude sessions),
`index_hidden` (int, count minus len(session_index)) and `session_index_truncated` (bool,
index_hidden > 0). Schema stays 2: these keys are additions, nothing was removed or retyped.

Several local LLM servers: `llms` is a list of the same objects `llm` holds, each with `name`, `label`, `age` and its own `stale`; `llms_hidden` counts servers past the cap. `llm` stays and is the first server, so a reader written before `llms` still works.

SIZE RULE: the file ALWAYS fits MAX_BYTES (2 MiB), measured on the encoded bytes. The file is
encoded with ensure_ascii=True, so it is pure ASCII and bytes equal characters, a non-ASCII
character costs 6 bytes (12 for a character outside the BMP) and the measurement counts that.
When over the bound, trim in this order: children per session to 20, children to 0, display
sessions, unattached group rows, runners, and as the LAST resort entries from the END of the
pid-sorted index. If even that cannot fit, a minimal document with empty lists is written.

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
from ..store import ensure_private_dir
from .view import Views, _s, project_label

SCHEMA_VERSION = 2
MAX_INDEX = 2000
_IDX_NAME = 48
_IDX_PROJECT = 48
_IDX_STATE = 16
_IDX_ID = 128
MAX_BYTES = 2 * 1024 * 1024  # well under the 4 MiB the mod runtime can read
_TRIM_STEPS = (20, 0)  # children kept per session, tried in order when the file is too big


def _finite(v: float | None) -> float | None:
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None


def _index_entry(session_id, name, pid, project, state, address) -> dict:
    return {
        "session_id": _s(session_id, _IDX_ID),
        "name": _s(name, _IDX_NAME),
        "pid": pid,
        "project": _s(project, _IDX_PROJECT),
        "state": _s(state, _IDX_STATE),
        "address": _s(address or session_id, _IDX_ID),
    }


def build_session_index(views: Views, snapshot: Snapshot | None) -> tuple[list[dict], int]:
    """(entries by pid capped at MAX_INDEX, true count of Claude sessions). Uses the snapshot when given
    (the view model hides sessions past its display cap); else the sessions the view shows."""
    if snapshot is not None:
        rows = [
            _index_entry(
                x.session_id, x.name, x.pid, project_label(x.project.value, x.project_source, _IDX_PROJECT),
                x.state, x.address,
            )
            for x in snapshot.sessions
            if x.agent == "claude" and x.pid != 0
        ]
    else:
        rows = [
            _index_entry(
                n.session_id, n.name, n.pid,
                project_label(n.project.removesuffix("~") if n.project_source == "last" else n.project,
                              n.project_source, _IDX_PROJECT),
                n.state, n.address,
            )
            for n in views.sessions
            if n.agent == "claude"
        ]
    rows.sort(key=lambda r: r["pid"])
    return rows[:MAX_INDEX], len(rows)


def views_to_dict(
    views: Views, *, snapshot: Snapshot | None = None, generated: float | None = None
) -> dict:
    """JSON-safe dict of the view model. is_current is always False and banner is None."""
    doc = asdict(views)
    doc["banner"] = None
    doc["age_s"] = _finite(views.age_s)
    for node in doc["sessions"]:
        node["is_current"] = False
        # The summary (collapsed, collapsed_names) stays; the folded rows are TUI-only.
        node.pop("helpers", None)
        node.pop("helpers_hidden", None)
    for group in doc["unattached"]:
        for node in group["sessions"]:
            node["is_current"] = False
            node.pop("helpers", None)
            node.pop("helpers_hidden", None)
    gen = _finite(time.time() if generated is None else generated)
    index, count = build_session_index(views, snapshot)
    return {
        "schema": SCHEMA_VERSION,
        "generated": gen,
        "session_index": index,
        "session_index_count": count,
        "index_hidden": count - len(index),
        "session_index_truncated": count > len(index),
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


def _halve(rows: list, hidden_owner: dict | None, hidden_key: str) -> None:
    cut = len(rows) // 2
    if hidden_owner is not None:
        hidden_owner[hidden_key] += len(rows) - cut
    del rows[cut:]


def _shrink_once(doc: dict) -> bool:
    """Halve the next non-empty display part. False when only the index is left to trim."""
    if doc["sessions"]:
        _halve(doc["sessions"], doc, "sessions_hidden")
        return True
    for g in doc["unattached"]:
        if g["procs"] or g["sessions"]:
            _halve(g["procs"], g, "hidden")
            _halve(g["sessions"], g, "hidden")
            return True
    if doc["runners"]:
        _halve(doc["runners"], doc, "runners_hidden")
        return True
    return False


def _set_index(doc: dict, full: list, k: int) -> None:
    doc["session_index"] = full[:k]
    doc["index_hidden"] = doc["session_index_count"] - k
    doc["session_index_truncated"] = doc["index_hidden"] > 0


def encode_bounded(doc: dict) -> bytes:
    """Encode to at most MAX_BYTES bytes, trimming in the order the module docstring states."""
    payload = _encode(doc)
    for keep in _TRIM_STEPS:
        if len(payload) <= MAX_BYTES:
            return payload
        _trim(doc, keep)
        payload = _encode(doc)
    while len(payload) > MAX_BYTES and _shrink_once(doc):
        payload = _encode(doc)
    if len(payload) <= MAX_BYTES:
        return payload
    full = doc["session_index"]  # last resort: drop index entries from the end (bisect)
    lo, hi = 0, len(full)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        _set_index(doc, full, mid)
        if len(_encode(doc)) <= MAX_BYTES:
            lo = mid
        else:
            hi = mid - 1
    _set_index(doc, full, lo)
    payload = _encode(doc)
    if len(payload) <= MAX_BYTES:
        return payload
    minimal = {
        k: doc[k]
        for k in ("schema", "generated", "status", "age_s", "message", "session_index_count")
    }
    minimal.update(
        session_index=[], index_hidden=doc["session_index_count"], session_index_truncated=True,
        banner=None, health=[], host={}, sessions=[], sessions_hidden=0, unattached=[],
        llm=None, llms=[], llms_hidden=0, runners=[], runners_hidden=0, quota=[],
    )
    return _encode(minimal)


def write_views(
    path: Path, views: Views, *, snapshot: Snapshot | None = None, generated: float | None = None
) -> None:
    """Atomically write views.json (temp file, fsync, 0600, os.replace). Raises OSError only."""
    path = Path(path)
    try:
        payload = encode_bounded(views_to_dict(views, snapshot=snapshot, generated=generated))
    except (ValueError, TypeError, RecursionError) as exc:
        raise OSError(f"views not encodable: {type(exc).__name__}") from None
    ensure_private_dir(path.parent)
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
