"""Source plugin contract: what a source receives and what it returns."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .config import SourceCfg
from .model import LlmServer, Proc, Runner, Session, Status


@dataclass(frozen=True)
class Ctx:
    """What a source may use: its own config slice, the clock, a logger."""

    cfg: SourceCfg
    clock: Callable[[], float]
    log: logging.Logger


@dataclass
class SourceResult:
    """Rows by kind. status overrides the default "ok" (for example "absent")."""

    sessions: list[Session] = field(default_factory=list)
    procs: list[Proc] = field(default_factory=list)
    runners: list[Runner] = field(default_factory=list)
    llms: list[LlmServer] = field(default_factory=list)
    llm: LlmServer | None = None  # older single-server form; llms wins when both are set
    host: dict = field(default_factory=dict)
    quota: dict = field(default_factory=dict)
    status: Status | None = None


class Source(Protocol):
    name: str

    def collect(self, ctx: Ctx) -> SourceResult: ...
