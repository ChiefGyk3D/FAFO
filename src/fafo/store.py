"""Atomic, owner-only snapshot file. Readers see the old file or the new one, never a mix.

Strict schema, SNAPSHOT_VERSION 2. Version 2 adds `llms` (a list of local LLM servers, each with
`name`, `last_ok` and `reachable`) and still writes `llm`, a copy of the first server, for readers
that look at that key. The reader accepts version 1 (no `llms`; its `llm` becomes one server named
"ollama") and version 2, where `llm` must equal `llms[0]`; any other version, or a version 1 file
with `llms`, is refused. A reader that only knows version 1 refuses a version 2 file (it checks the
version and rejects unknown keys), by design.

A session's `project_source` key is written only when it is not "live", under either version. The
reader accepts both forms; a reader that predates the key refuses a snapshot that contains a
last-known or folder project (it rejects unknown keys) by design.
"""

from __future__ import annotations

import json
import logging
import math
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import fields
from pathlib import Path
from typing import get_args

from .jsonsafe import MAX_INT_BITS, check_finite, plain_copy
from .model import (
    MAX_LLM_SERVERS,
    OLD_SNAPSHOT_VERSION,
    SNAPSHOT_VERSION,
    Inflight,
    JobRow,
    JobSummary,
    LlmServer,
    LoadedModel,
    Proc,
    ProjectSource,
    Runner,
    Session,
    Snapshot,
    SourceHealth,
    Stamped,
    Status,
)

_log = logging.getLogger(__name__)


def _ensure_dir(directory: Path) -> None:
    """Create missing directories as 0700. An existing directory is never chmodded."""
    missing: list[Path] = []
    cur = directory
    while not cur.exists() and cur != cur.parent:
        missing.append(cur)
        cur = cur.parent
    for d in reversed(missing):
        try:
            os.mkdir(d, stat.S_IRWXU)
        except FileExistsError:
            continue  # someone else created it: not ours to chmod
        os.chmod(d, stat.S_IRWXU)


def ensure_private_dir(directory: Path) -> None:
    """Public: create missing directories as owner-only (0700); never chmod an existing one."""
    _ensure_dir(Path(directory))


def _omit_default_source(plain) -> None:
    """Leave out the default project_source ("live") so a snapshot with no last-known project keeps
    the exact key set older readers accept. A snapshot that does carry one (idle session showing its
    last repo, or a folder name) is refused by older readers by design: the schema is strict."""
    sessions = plain.get("sessions") if isinstance(plain, dict) else None
    if isinstance(sessions, list):
        for s in sessions:
            if isinstance(s, dict) and s.get("project_source") == "live":
                del s["project_source"]


# Largest snapshot file the reader accepts: twice the 2 MiB export cap, since a snapshot carries
# every source's rows before the export trims them. Anything bigger is refused unread.
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024


def write_snapshot(
    path: Path, snap: Snapshot, *, _before_replace: Callable[[], None] | None = None
) -> None:
    """Write via a temp file in the same directory, chmod 0600, then os.replace.

    _before_replace is a test seam called after the temp file is complete and before the rename.
    """
    path = Path(path)
    ensure_private_dir(path.parent)
    # Iterative, capped walk: NaN, infinities and over-deep or over-large values become null, so
    # the file is strict JSON that read_snapshot accepts. Log the count only, never the values.
    plain, replaced = plain_copy(snap)
    if replaced:
        _log.warning("snapshot: sanitized %d values (nulled, dropped or truncated)", replaced)
    _omit_default_source(plain)
    if plain.get("jobs") is None:
        plain.pop("jobs", None)  # additive key: absent unless a jobs source ran
    plain["llm"] = plain["llms"][0] if plain["llms"] else None  # first server, for older readers
    payload = _encode(plain)
    if len(payload) > MAX_SNAPSHOT_BYTES:
        payload = _trim_to_fit(plain)
    if len(payload) > MAX_SNAPSHOT_BYTES:
        # Nothing left to shrink: the reader would refuse this file, so keep the last good one.
        _log.warning(
            "snapshot: still %d bytes over the %d byte cap after shrinking; kept the previous file",
            len(payload) - MAX_SNAPSHOT_BYTES, MAX_SNAPSHOT_BYTES,
        )
        return
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        if _before_replace is not None:
            _before_replace()
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _encode(plain: dict) -> bytes:
    return json.dumps(plain, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _trim_to_fit(plain: dict) -> bytes:
    """Shrink an over-cap document to MAX_SNAPSHOT_BYTES, never raising.

    The reader refuses a file over the cap, so the writer must not produce one. Rows are dropped
    from the tail, in this order: processes attached to no session, then processes of sessions
    (last sessions first), then runner rows. Sessions and health are never dropped. The smallest
    number of dropped rows that fits is found by bisection (at most about 20 encodes).
    """
    procs = plain["procs"]
    order = [i for i in range(len(procs) - 1, -1, -1) if procs[i].get("session_pid") is None]
    order += [i for i in range(len(procs) - 1, -1, -1) if procs[i].get("session_pid") is not None]
    runners = plain["runners"]
    total = len(order) + len(runners)

    def build(k: int) -> bytes:
        gone = set(order[: min(k, len(order))])
        doc = dict(plain)
        doc["procs"] = [r for i, r in enumerate(procs) if i not in gone]
        doc["runners"] = runners[: max(0, len(runners) - max(0, k - len(order)))]
        return _encode(doc)

    lo, hi = 0, total  # invariant: hi fits, or hi is the smallest document we can make
    while lo < hi:
        mid = (lo + hi) // 2
        if len(build(mid)) <= MAX_SNAPSHOT_BYTES:
            hi = mid
        else:
            lo = mid + 1
    _log.warning("snapshot: trimmed %d rows to fit", hi)
    payload = build(hi)
    if len(payload) <= MAX_SNAPSHOT_BYTES:
        return payload
    # Rows are gone and the rest is still too big: shrink the sections that cannot be dropped.
    base = dict(plain)
    base["procs"] = []
    base["runners"] = []
    for passes, (items, chars) in enumerate(_SHRINK_PASSES, 1):
        payload = _encode(_shrink_doc(base, items, chars))
        if len(payload) <= MAX_SNAPSHOT_BYTES:
            _log.warning("snapshot: shrank non-row data in %d passes", passes)
            return payload
    payload = _encode(_identity_only(base))
    if len(payload) <= MAX_SNAPSHOT_BYTES:
        _log.warning("snapshot: kept only row identities to fit")
    return payload


# (nested list and mapping entries kept, string characters kept) per pass, mildest first. A fixed
# number of passes, each one re-encoded: the work is bounded however hostile the input. These
# passes never remove or reshape a session or health row; they cut only content inside rows and
# the free-form host, quota and gpu data.
_SHRINK_PASSES = ((256, 2048), (64, 512), (16, 128), (4, 32), (1, 16))
# Identity strings (session id, address, name, project) are cut to this many characters, always.
_IDENT_CHARS = 256
# Last resort only: the most session rows and the most health rows kept. A snapshot with more
# rows than this loses the excess tail rows, and only when everything else has failed to fit.
MAX_KEPT_ROWS = 4096
# Kept whole (up to _IDENT_CHARS): identity, plus short enumerations the reader checks.
_SESSION_IDENT = ("session_id", "address", "name", "project_source")
_HEALTH_IDENT = ("name", "status")


def _shrink_value(v, items: int, chars: int, depth: int = 0):
    """Copy of a free-form JSON value with long lists, mappings and strings cut. Depth is already
    capped by plain_copy; the guard here keeps the walk bounded regardless."""
    if depth > 40:
        return None
    if type(v) is str:
        return v[:chars]
    if type(v) is list:
        return [_shrink_value(x, items, chars, depth + 1) for x in v[:items]]
    if type(v) is dict:
        out = {}
        for k, x in list(v.items())[:items]:
            out[k[:chars]] = _shrink_value(x, items, chars, depth + 1)
        return out
    return v


def _shrink_row(row, chars: int, keep=()):
    """A row with every key kept (the reader insists on the exact key set) and its strings cut.
    Strings named in keep are identity and are cut only to _IDENT_CHARS."""
    if type(row) is not dict:
        return row
    out = {}
    for k, v in row.items():
        if type(v) is str:
            out[k] = v[:_IDENT_CHARS if k in keep else chars]
        elif type(v) is dict:  # the stamped project: its value is identity
            out[k] = _shrink_row(v, _IDENT_CHARS if k == "project" else chars, ("value",))
        else:
            out[k] = v
    return out


def _shrink_doc(plain: dict, items: int, chars: int) -> dict:
    """Shrink content only: the session, health and server lists keep every row."""
    doc = dict(plain)
    doc["host"] = _shrink_value(plain["host"], items, chars)
    doc["quota"] = _shrink_value(plain["quota"], items, chars)
    doc["llms"] = [
        {
            **srv,
            "models": [_shrink_row(m, chars) for m in srv["models"][:items]],
            "inflight": [_shrink_row(m, chars) for m in srv["inflight"][:items]],
            "gpu": _shrink_value(srv["gpu"], items, chars),
        }
        for srv in plain["llms"]
    ]
    doc["llm"] = doc["llms"][0] if doc["llms"] else None
    doc["sessions"] = [_shrink_row(x, chars, _SESSION_IDENT) for x in plain["sessions"]]
    doc["health"] = [_shrink_row(x, chars, _HEALTH_IDENT) for x in plain["health"]]
    return doc


def _identity_only(plain: dict) -> dict:
    """Last resort: every session and health row keeps its identity and loses every other string;
    host, quota, gpu, models and in-flight lists are emptied. At most MAX_KEPT_ROWS rows of each."""
    doc = _shrink_doc(plain, 0, 0)
    doc["sessions"] = [
        _shrink_row(x, 0, _SESSION_IDENT) for x in plain["sessions"][:MAX_KEPT_ROWS]
    ]
    doc["health"] = [_shrink_row(x, 0, _HEALTH_IDENT) for x in plain["health"][:MAX_KEPT_ROWS]]
    return doc


_MAX_NAME = 100  # longest identity name kept (LLM server and health row names)


def _int(v):
    if type(v) is not int or v < 0 or v.bit_length() > MAX_INT_BITS:
        raise TypeError("expected non-negative int")


def _opt_int(v):
    if v is not None:
        _int(v)


def _float(v):
    if type(v) is int and v.bit_length() > MAX_INT_BITS:
        raise TypeError("expected finite number")  # before math.isfinite: it overflows on big ints
    if type(v) not in (int, float) or not math.isfinite(v):
        raise TypeError("expected finite number")


def _str(v):
    if type(v) is not str:
        raise TypeError("expected str")


def _dict(v):
    if type(v) is not dict:
        raise TypeError("expected dict")
    check_finite(v)


def _constant(name):
    """NaN and Infinity tokens parse to floats, so a typed field holding one is repaired like any
    other bad number. Untyped dicts (host, quota, gpu) still refuse them in check_finite."""
    return float(name)


def _status(v):
    if type(v) is not str or v not in get_args(Status):
        raise TypeError("bad status")


def _project_source(v):
    if type(v) is not str or v not in get_args(ProjectSource):
        raise TypeError("bad project_source")


def _health_name(v):
    if type(v) is not str or len(v) > _MAX_NAME:
        raise TypeError("bad health name")


def _job_state(v):
    if type(v) is not str or v not in ("running", "done", "failed"):
        raise TypeError("bad job state")


_MAX_JOB_ROWS = 64
_JOB_COUNTS = {"running": _int, "done": _int, "failed": _int, "hidden": _int, "invalid": _int,
               "unread": _int}


def _jobs(data, tally) -> JobSummary:
    """The optional jobs section. Rows with a bad state are refused with the file (no identity
    to drop by); a bad count is repaired to 0 like any typed number."""
    out, _ = _check(JobSummary, data, {**_JOB_COUNTS, "rows": lambda v: None}, tally)
    rows = out["rows"]
    if type(rows) is not list or len(rows) > _MAX_JOB_ROWS:
        raise TypeError("bad job rows")
    return JobSummary(**{**out, "rows": [_build(JobRow, r, tally) for r in rows]})


# Explicit scalar checks per dataclass; the key sets must match the dataclass fields exactly.
_SPEC = {
    Stamped: {"value": _str, "as_of": _float},
    Proc: {
        "pid": _int, "ppid": _int, "session_pid": _opt_int, "agent": _str, "cmd": _str,
        "cpu": _float, "rss": _int, "started": _float,
    },
    LoadedModel: {"name": _str, "vram": _int, "context": _int, "expires": _float},
    Inflight: {"model": _str, "session_pid": _opt_int, "started": _float},
    Runner: {
        "name": _str, "state": _str, "repo": _str, "workflow": _str, "job": _str,
        "step": _str, "step_n": _int, "step_total": _int, "elapsed": _float,
        "load": _float, "mem": _float, "disk": _float,
    },
    SourceHealth: {"name": _health_name, "status": _status, "last_ok": _float, "error": _str},
    JobRow: {"state": _job_state, "age": _float, "kind": _str, "phase": _str, "title": _str},
}
_SESSION_SPEC = {
    "pid": _int, "agent": _str, "session_id": _str, "project_rule": _str, "task": _str,
    "state": _str, "branch": _str, "worktree": _str, "started": _float, "cpu": _float,
    "rss": _int, "name": _str, "address": _str, "project_source": _project_source,
}
_SESSION_OPTIONAL = {"name": "", "address": "", "project_source": "live"}  # absent in older snapshots


class _Drop(Exception):
    """A row whose identity field is bad: the row is dropped, never the file."""


class _Tally:
    """Counts only (never values) of what the reader repaired or dropped."""

    def __init__(self) -> None:
        self.fixed = 0
        self.dropped = 0


# A typed number that is null, non-finite or too wide is repaired to its documented default.
_DEFAULTS = {_int: 0, _opt_int: None, _float: 0.0}
# Identity fields: a row with a bad one is dropped (counted), because a default would invent a row.
_IDENTITY = {
    Proc: {"pid"},
    Session: {"pid", "session_id"},
    Runner: {"name"},
    LlmServer: {"name"},
    SourceHealth: {"name"},
}


def _check(cls, data, spec, tally):
    """Require a dict with exactly the dataclass fields. Valid scalars pass through; a bad typed
    number becomes its default (counted); any other bad value refuses the file (TypeError). A bad
    identity field is only FLAGGED: the whole row (and its nested containers) must still validate,
    so structural damage anywhere in a row refuses the file even when its identity is bad.
    Returns (repaired copy, identity_bad); the caller drops the row after the full validation."""
    if type(data) is not dict:
        raise TypeError("expected object")
    if set(data) != {f.name for f in fields(cls)}:
        raise TypeError("field mismatch")
    out = dict(data)
    identity_bad = False
    for name, check in spec.items():
        try:
            check(out[name])
        except TypeError:
            if name in _IDENTITY.get(cls, ()):
                identity_bad = True
                continue
            if check not in _DEFAULTS:
                raise
            out[name] = _DEFAULTS[check]
            tally.fixed += 1
    return out, identity_bad


def _build(cls, data, tally):
    out, identity_bad = _check(cls, data, _SPEC[cls], tally)
    if identity_bad:
        raise _Drop
    return cls(**out)


def _rows(build, data, tally):
    """Build every row; a row with a bad identity is dropped and counted. Entries are None for
    dropped rows (so positions stay known); callers filter."""
    if type(data) is not list:
        raise TypeError("expected list")
    out = []
    for item in data:
        try:
            out.append(build(item))
        except _Drop:
            tally.dropped += 1
            out.append(None)
    return out


def _list(cls, data, tally):
    return [r for r in _rows(lambda item: _build(cls, item, tally), data, tally) if r is not None]


def _session(data, tally) -> Session:
    if type(data) is not dict:
        raise TypeError("bad session")
    data = {**_SESSION_OPTIONAL, **data}
    out, identity_bad = _check(Session, data, {**_SESSION_SPEC, "project": lambda v: None}, tally)
    project = _build(Stamped, out["project"], tally)  # validated before any drop
    if identity_bad:
        raise _Drop
    return Session(**{**out, "project": project})


def _session_list(data, tally) -> list[Session]:
    rows = _rows(lambda item: _session(item, tally), data, tally)
    return [r for r in rows if r is not None]


_LLM_NEW = {"name": "ollama", "last_ok": 0.0, "reachable": True}  # absent in version 1
_MAX_LLM_NAME = 100


def _name(v):
    if type(v) is not str or not 0 < len(v) <= _MAX_LLM_NAME:
        raise TypeError("bad server name")


def _bool(v):
    if type(v) is not bool:
        raise TypeError("expected bool")


def _llm(data, tally, *, old: bool = False) -> LlmServer | None:
    if data is None:
        return None
    if old and type(data) is dict:
        data = {**_LLM_NEW, **data}
    out, identity_bad = _check(
        LlmServer, data,
        {"queue": _int, "gpu": _dict, "name": _name, "last_ok": _float, "reachable": _bool},
        tally,
    )
    server = LlmServer(  # nested rows are validated here, before any drop decision
        models=_list(LoadedModel, out["models"], tally),
        queue=out["queue"],
        inflight=_list(Inflight, out["inflight"], tally),
        gpu=out["gpu"],
        name=out["name"],
        last_ok=out["last_ok"],
        reachable=out["reachable"],
    )
    if identity_bad:
        raise _Drop
    return server


def _llms(doc, old: bool, tally) -> list[LlmServer]:
    """Version 1 has only "llm" (one server, no name). Version 2 has "llms" and "llm", and the
    two must agree: "llm" is a copy of the first server kept for older readers. A server with a
    bad name is dropped; "llm" is compared with the first listed server after the same repair
    (both dropped, or both equal)."""
    # "llm" is a copy: repair it with a scratch tally so its defects are not counted twice.
    try:
        first = _llm(doc["llm"], _Tally(), old=old)
    except _Drop:
        first = None
    if old:
        if first is None:
            if doc["llm"] is not None:
                tally.dropped += 1
            return []
        return [_llm(doc["llm"], tally, old=True)]
    raw = doc["llms"]
    if type(raw) is not list or len(raw) > MAX_LLM_SERVERS:
        raise TypeError("bad llms")
    servers: list[LlmServer | None] = []
    for item in raw:
        if item is None:
            raise TypeError("null server in llms")
        try:
            servers.append(_llm(item, tally))
        except _Drop:
            tally.dropped += 1
            servers.append(None)
    if first != (servers[0] if servers else None):
        raise TypeError("llm does not match llms")
    return [s for s in servers if s is not None]


def _snapshot(doc, tally) -> Snapshot:
    if not isinstance(doc, dict):
        raise TypeError("expected object")
    version = doc.get("version")
    if type(version) is not int or version not in (OLD_SNAPSHOT_VERSION, SNAPSHOT_VERSION):
        raise ValueError("unsupported version")
    old = version == OLD_SNAPSHOT_VERSION
    want = {f.name for f in fields(Snapshot)} | {"llm"}
    if "jobs" not in doc:
        want.discard("jobs")  # omitted when no jobs source ran
    if old:
        want -= {"llms"}
    if set(doc) != want:
        raise TypeError("field mismatch")
    taken = doc["taken"]
    try:
        _float(taken)
    except TypeError:
        taken, tally.fixed = 0.0, tally.fixed + 1  # a bad clock reads as very old, not as a refusal
    _dict(doc["host"])
    _dict(doc["quota"])
    return Snapshot(
        version=SNAPSHOT_VERSION,
        taken=taken,
        host=doc["host"],
        sessions=_session_list(doc["sessions"], tally),
        procs=_list(Proc, doc["procs"], tally),
        llms=_llms(doc, old, tally),
        runners=_list(Runner, doc["runners"], tally),
        quota=doc["quota"],
        health=_list(SourceHealth, doc["health"], tally),
        jobs=_jobs(doc["jobs"], tally) if "jobs" in doc else None,
    )


_OPEN_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC


def _read_bounded(path: Path) -> bytes:
    """Read the file at `path` or raise OSError: a regular, owner-only file within the size cap."""
    fd = os.open(path, _OPEN_FLAGS)  # no-follow and non-blocking: a FIFO or link cannot hang us
    try:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) & (stat.S_IRWXG | stat.S_IRWXO)
            or st.st_size > MAX_SNAPSHOT_BYTES
        ):
            raise OSError("snapshot file refused")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_SNAPSHOT_BYTES:
            chunk = os.read(fd, min(65536, MAX_SNAPSHOT_BYTES + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
        raise OSError("snapshot file too large")
    finally:
        os.close(fd)


def read_snapshot(path: Path) -> Snapshot | None:
    """Return the snapshot, or None if missing, truncated, invalid or an unknown version."""
    try:
        doc = json.loads(_read_bounded(path), parse_constant=_constant)
        tally = _Tally()
        snap = _snapshot(doc, tally)
        if tally.fixed or tally.dropped:
            # Counts only, never values: one bad number must not blank the display.
            _log.warning(
                "snapshot: repaired %d values, dropped %d rows", tally.fixed, tally.dropped
            )
        return snap
    except (
        OSError, ValueError, TypeError, KeyError, IndexError, AttributeError,
        ArithmeticError, RecursionError, RuntimeError, LookupError, AssertionError,
    ) as exc:  # fail closed: parse or validation surprises are a refusal (MemoryError is not);
        # an unexpected error class from a row validator is a refusal too, never a raise
        # (a blanket `except Exception` is refused by the BLE001 lint, so the classes are named)
        _log.debug("snapshot unreadable: %s", type(exc).__name__)
        return None
