"""Atomic, owner-only snapshot file. Readers see the old file or the new one, never a mix."""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import asdict, fields
from pathlib import Path
from typing import get_args

from .jsonsafe import check_finite
from .model import (
    MAX_LLM_SERVERS,
    OLD_SNAPSHOT_VERSION,
    SNAPSHOT_VERSION,
    Inflight,
    LlmServer,
    LoadedModel,
    Proc,
    Runner,
    Session,
    Snapshot,
    SourceHealth,
    Stamped,
    Status,
)


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
    doc = asdict(snap)
    doc["llm"] = doc["llms"][0] if doc["llms"] else None  # first server, for older readers
    payload = json.dumps(doc, separators=(",", ":")).encode("utf-8")
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
    if type(v) is not int or v < 0:
        raise TypeError("expected non-negative int")


def _opt_int(v):
    if v is not None:
        _int(v)


def _float(v):
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
    "rss": _int, "name": _str, "address": _str,
}
_SESSION_OPTIONAL = {"name": "", "address": ""}  # absent in older snapshots


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


_LLM_NEW = {"name": "ollama", "last_ok": 0.0, "reachable": True}  # absent in version 1
_MAX_LLM_NAME = 100


def _name(v):
    if type(v) is not str or not 0 < len(v) <= _MAX_LLM_NAME:
        raise TypeError("bad server name")


def _bool(v):
    if type(v) is not bool:
        raise TypeError("expected bool")


def _llm(data, *, old: bool = False) -> LlmServer | None:
    if data is None:
        return None
    if old and type(data) is dict:
        data = {**_LLM_NEW, **data}
    _check(
        LlmServer, data,
        {"queue": _int, "gpu": _dict, "name": _name, "last_ok": _float, "reachable": _bool},
    )
    return LlmServer(
        models=_list(LoadedModel, data["models"]),
        queue=data["queue"],
        inflight=_list(Inflight, data["inflight"]),
        gpu=data["gpu"],
        name=data["name"],
        last_ok=data["last_ok"],
        reachable=data["reachable"],
    )


def _llms(doc, old: bool) -> list[LlmServer]:
    """Version 1 has only "llm" (one server, no name). Version 2 has "llms" and "llm", and the
    two must agree: "llm" is a copy of the first server kept for older readers."""
    first = _llm(doc["llm"], old=old)
    if old:
        return [] if first is None else [first]
    raw = doc["llms"]
    if type(raw) is not list or len(raw) > MAX_LLM_SERVERS:
        raise TypeError("bad llms")
    servers: list[LlmServer] = []
    for item in raw:
        s = _llm(item)
        if s is None:
            raise TypeError("null server in llms")
        servers.append(s)
    if first != (servers[0] if servers else None):
        raise TypeError("llm does not match llms")
    return servers


def _snapshot(doc) -> Snapshot:
    if not isinstance(doc, dict):
        raise TypeError("expected object")
    version = doc.get("version")
    if type(version) is not int or version not in (OLD_SNAPSHOT_VERSION, SNAPSHOT_VERSION):
        raise ValueError("unsupported version")
    old = version == OLD_SNAPSHOT_VERSION
    want = {f.name for f in fields(Snapshot)} | {"llm"}
    if old:
        want -= {"llms"}
    if set(doc) != want:
        raise TypeError("field mismatch")
    _float(doc["taken"])
    _dict(doc["host"])
    _dict(doc["quota"])
    return Snapshot(
        version=SNAPSHOT_VERSION,
        taken=doc["taken"],
        host=doc["host"],
        sessions=_session_list(doc["sessions"]),
        procs=_list(Proc, doc["procs"]),
        llms=_llms(doc, old),
        runners=_list(Runner, doc["runners"]),
        quota=doc["quota"],
        health=_list(SourceHealth, doc["health"]),
    )


def read_snapshot(path: Path) -> Snapshot | None:
    """Return the snapshot, or None if missing, truncated, invalid or an unknown version."""
    try:
        doc = json.loads(Path(path).read_bytes(), parse_constant=_refuse_constant)
        return _snapshot(doc)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None
