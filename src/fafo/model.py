"""Typed rows and the snapshot container. Plain data only, no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, Literal, TypeVar

T = TypeVar("T")

# 2: Snapshot.llms (several local LLM servers) and LlmServer.name/last_ok/reachable. The file
# still carries "llm" (the first server) for older readers. Version 1 files are still read.
SNAPSHOT_VERSION = 2
OLD_SNAPSHOT_VERSION = 1
DEFAULT_LLM_NAME = "ollama"
MAX_LLM_SERVERS = 8

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
    # Which server these rows describe; time of the last good poll (0.0 = never);
    # False when the latest poll failed (the rows are then the last good ones, kept).
    name: str = DEFAULT_LLM_NAME
    last_ok: float = 0.0
    reachable: bool = True


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
class JobRow:
    """One Codex plugin job. title is empty unless the source option show_titles is set."""

    state: str  # running | done | failed
    age: float  # seconds since start (running) or finish (done, failed)
    kind: str
    phase: str
    title: str


@dataclass
class JobSummary:
    """Counts are always set; rows are bounded and hold titles only when the option allows."""

    running: int = 0
    done: int = 0
    failed: int = 0
    hidden: int = 0  # classified rows past the row cap
    invalid: int = 0  # files refused as unsafe or malformed
    unread: int = 0  # directory entries not examined (past the caps)
    rows: list[JobRow] = field(default_factory=list)


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
    llms: list[LlmServer] = field(default_factory=list)
    runners: list[Runner] = field(default_factory=list)
    quota: dict = field(default_factory=dict)
    health: list[SourceHealth] = field(default_factory=list)
    jobs: JobSummary | None = None  # Codex plugin jobs; left out of the file when None

    @property
    def llm(self) -> LlmServer | None:
        """The first server, for readers written before several servers were supported."""
        return self.llms[0] if self.llms else None
