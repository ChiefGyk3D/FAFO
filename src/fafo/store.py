"""Atomic, owner-only snapshot file. Readers see the old file or the new one, never a mix."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, fields
from pathlib import Path

from .model import (
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
)


def write_snapshot(path: Path, snap: Snapshot) -> None:
    """Write via a temp file in the same directory, chmod 0600, then os.replace."""
    path = Path(path)
    payload = json.dumps(asdict(snap), separators=(",", ":")).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), 0o600)
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


def _build(cls, data):
    """Build a dataclass from a dict with exactly its fields, else raise TypeError."""
    if not isinstance(data, dict):
        raise TypeError("expected object")
    names = {f.name for f in fields(cls)}
    if set(data) != names:
        raise TypeError("field mismatch")
    return cls(**data)


def _list(cls, data):
    if not isinstance(data, list):
        raise TypeError("expected list")
    return [_build(cls, item) for item in data]


def _session(data) -> Session:
    if not isinstance(data, dict) or not isinstance(data.get("project"), dict):
        raise TypeError("bad session")
    data = {**data, "project": _build(Stamped, data["project"])}
    return _build(Session, data)


def _llm(data) -> LlmServer | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise TypeError("bad llm")
    data = {
        **data,
        "models": _list(LoadedModel, data.get("models")),
        "inflight": _list(Inflight, data.get("inflight")),
    }
    return _build(LlmServer, data)


def _snapshot(doc) -> Snapshot:
    if not isinstance(doc, dict):
        raise TypeError("expected object")
    version = doc.get("version")
    if type(version) is not int or version != SNAPSHOT_VERSION:
        raise ValueError("unsupported version")
    if set(doc) != {f.name for f in fields(Snapshot)}:
        raise TypeError("field mismatch")
    sessions = doc["sessions"]
    if not isinstance(sessions, list):
        raise TypeError("bad sessions")
    return Snapshot(
        version=version,
        taken=doc["taken"],
        host=doc["host"],
        sessions=[_session(s) for s in sessions],
        procs=_list(Proc, doc["procs"]),
        llm=_llm(doc["llm"]),
        runners=_list(Runner, doc["runners"]),
        quota=doc["quota"],
        health=_list(SourceHealth, doc["health"]),
    )


def read_snapshot(path: Path) -> Snapshot | None:
    """Return the snapshot, or None if missing, truncated, invalid or an unknown version."""
    try:
        doc = json.loads(Path(path).read_bytes())
        return _snapshot(doc)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None
