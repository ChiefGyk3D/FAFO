"""Typed rows and the snapshot container. Plain data only, no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, Literal, TypeVar

T = TypeVar("T")

SNAPSHOT_VERSION = 1

Status = Literal["ok", "stale", "down", "absent"]
ProjectSource = Literal["live", "last", "folder"]


@dataclass(frozen=True)
class Stamped(Generic[T]):
    """A value with the time it was last observed (epoch seconds)."""

    value: T
    as_of: float


@dataclass
class Session:
    pid: int
    agent: str
    session_id: str
    project: Stamped[str]
    project_rule: str
    task: str
    state: str
    branch: str
    worktree: str
    started: float
    cpu: float
    rss: int
    name: str = ""
    address: str = ""  # full session id, for manual cross-session messaging
    project_source: ProjectSource = "live"  # live repo, last-known repo (idle), or folder name


@dataclass
class Proc:
    pid: int
    ppid: int
    session_pid: int | None
    agent: str
    cmd: str  # already redacted by the collector
    cpu: float
    rss: int
    started: float


@dataclass
class LoadedModel:
    name: str
    vram: int
    context: int
    expires: float


@dataclass
class Inflight:
    model: str
    session_pid: int | None
    started: float


@dataclass
class LlmServer:
    models: list[LoadedModel]
    queue: int
    inflight: list[Inflight]
    gpu: dict


@dataclass
class Runner:
    name: str
    state: str
    repo: str
    workflow: str
    job: str
    step: str
    step_n: int
    step_total: int
    elapsed: float
    load: float
    mem: float
    disk: float


@dataclass
class SourceHealth:
    name: str
    status: Status
    last_ok: float
    error: str


@dataclass
class Snapshot:
    version: int
    taken: float
    host: dict
    sessions: list[Session] = field(default_factory=list)
    procs: list[Proc] = field(default_factory=list)
    llm: LlmServer | None = None
    runners: list[Runner] = field(default_factory=list)
    quota: dict = field(default_factory=dict)
    health: list[SourceHealth] = field(default_factory=list)
