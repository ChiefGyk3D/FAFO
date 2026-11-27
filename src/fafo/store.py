"""Atomic, owner-only snapshot file. Readers see the old file or the new one, never a mix."""

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
    SNAPSHOT_VERSION,
    Inflight,
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
    payload = json.dumps(plain, separators=(",", ":"), allow_nan=False).encode("utf-8")
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


def _refuse_constant(name):
    raise ValueError("non-standard JSON constant")


def _status(v):
    if type(v) is not str or v not in get_args(Status):
        raise TypeError("bad status")


def _project_source(v):
    if type(v) is not str or v not in get_args(ProjectSource):
        raise TypeError("bad project_source")


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
    SourceHealth: {"name": _str, "status": _status, "last_ok": _float, "error": _str},
}
_SESSION_SPEC = {
    "pid": _int, "agent": _str, "session_id": _str, "project_rule": _str, "task": _str,
    "state": _str, "branch": _str, "worktree": _str, "started": _float, "cpu": _float,
    "rss": _int, "name": _str, "address": _str, "project_source": _project_source,
}
_SESSION_OPTIONAL = {"name": "", "address": "", "project_source": "live"}  # absent in older snapshots


def _check(cls, data, spec):
    """Require a dict with exactly the dataclass fields and valid scalar values."""
    if type(data) is not dict:
        raise TypeError("expected object")
    if set(data) != {f.name for f in fields(cls)}:
        raise TypeError("field mismatch")
    for name, check in spec.items():
        check(data[name])


def _build(cls, data):
    _check(cls, data, _SPEC[cls])
    return cls(**data)


def _list(cls, data):
    if type(data) is not list:
        raise TypeError("expected list")
    return [_build(cls, item) for item in data]


def _session(data) -> Session:
    if type(data) is not dict:
        raise TypeError("bad session")
    data = {**_SESSION_OPTIONAL, **data}
    _check(Session, data, {**_SESSION_SPEC, "project": lambda v: None})
    return Session(**{**data, "project": _build(Stamped, data["project"])})


def _session_list(data) -> list[Session]:
    if type(data) is not list:
        raise TypeError("expected list")
    return [_session(item) for item in data]


def _llm(data) -> LlmServer | None:
    if data is None:
        return None
    _check(LlmServer, data, {"queue": _int, "gpu": _dict})
    return LlmServer(
        models=_list(LoadedModel, data["models"]),
        queue=data["queue"],
        inflight=_list(Inflight, data["inflight"]),
        gpu=data["gpu"],
    )


def _snapshot(doc) -> Snapshot:
    if not isinstance(doc, dict):
        raise TypeError("expected object")
    version = doc.get("version")
    if type(version) is not int or version != SNAPSHOT_VERSION:
        raise ValueError("unsupported version")
    if set(doc) != {f.name for f in fields(Snapshot)}:
        raise TypeError("field mismatch")
    _float(doc["taken"])
    _dict(doc["host"])
    _dict(doc["quota"])
    return Snapshot(
        version=version,
        taken=doc["taken"],
        host=doc["host"],
        sessions=_session_list(doc["sessions"]),
        procs=_list(Proc, doc["procs"]),
        llm=_llm(doc["llm"]),
        runners=_list(Runner, doc["runners"]),
        quota=doc["quota"],
        health=_list(SourceHealth, doc["health"]),
    )


def read_snapshot(path: Path) -> Snapshot | None:
    """Return the snapshot, or None if missing, truncated, invalid or an unknown version."""
    try:
        doc = json.loads(Path(path).read_bytes(), parse_constant=_refuse_constant)
        return _snapshot(doc)
    except (
        OSError, ValueError, TypeError, KeyError, IndexError, AttributeError,
        ArithmeticError, RecursionError,
    ) as exc:  # fail closed: parse or validation surprises are a refusal (MemoryError is not)
        _log.debug("snapshot unreadable: %s", type(exc).__name__)
        return None
