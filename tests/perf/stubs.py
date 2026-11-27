"""Stub sources and a child entry point that runs a REAL Collector.

The measurement code starts this file as its own process, so the CPU and memory it reads
belong to the collector alone. Nothing here touches the network or the real machine state.

    python stubs.py --dir DIR --mode idle|active|stress --tick 1.0
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

SESSIONS = 7
IDLE_CHILDREN = 20
ACTIVE_CHILDREN = 256
HUNG_PID = 90001
SNAPSHOT_NAME = "snapshot.json"
VIEWS_NAME = "views.json"


def _make_session(pid: int, n: int):
    from fafo.model import Session, Stamped

    return Session(
        pid=pid, agent="claude", session_id=f"sess-{pid}", project=Stamped("proj", time.time()),
        project_rule="cwd-name", task=f"task {n}", state="busy", branch="main",
        worktree="wt", started=1.0, cpu=float(n % 50), rss=100_000_000 + n, name=f"s{pid}",
    )


def _make_proc(pid: int, session_pid: int, n: int):
    from fafo.model import Proc

    return Proc(pid, session_pid, session_pid, "claude", f"tool --arg {pid}", float(n % 100),
                1_000_000 + n, 1.0)


class AgentsSource:
    """7 sessions with `children` children each. With `change`, every row moves each call."""

    name = "agents"

    def __init__(self, children: int, change: bool):
        self.children = children
        self.change = change
        self.n = 0

    def collect(self, ctx):
        from fafo.source import SourceResult

        self.n += 1
        k = self.n if self.change else 0
        sessions, procs = [], []
        for i in range(SESSIONS):
            spid = 1000 + i
            sessions.append(_make_session(spid, k))
            for j in range(self.children):
                procs.append(_make_proc(10_000 + i * 1000 + j, spid, k + j))
        return SourceResult(sessions=sessions, procs=procs)


class HostSource:
    name = "host"

    def collect(self, ctx):
        from fafo.source import SourceResult

        return SourceResult(host={"load1": 0.5, "cpu_percent": 3.0})


class HungSource:
    """Reports one session on the first call, then blocks forever on every later call."""

    name = "hung"

    def __init__(self):
        self.calls = 0

    def collect(self, ctx):
        from fafo.source import SourceResult

        self.calls += 1
        if self.calls == 1:
            return SourceResult(sessions=[_make_session(HUNG_PID, 0)])
        while True:
            time.sleep(3600)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--mode", choices=("idle", "active", "stress"), required=True)
    ap.add_argument("--tick", type=float, default=2.0)
    args = ap.parse_args(argv)

    from fafo.collector import Collector
    from fafo.config import Config, SourceCfg

    out = Path(args.dir)
    active = args.mode == "active"
    sources = [
        AgentsSource(ACTIVE_CHILDREN if active else IDLE_CHILDREN, change=active),
        HostSource(),
    ]
    scfg = {
        "agents": SourceCfg(enabled=True, interval_s=2.0, timeout_s=5.0),
        "host": SourceCfg(enabled=True, interval_s=2.0, timeout_s=5.0),
    }
    if args.mode == "stress":
        sources.append(HungSource())
        scfg["hung"] = SourceCfg(enabled=True, interval_s=2.0, timeout_s=1.5)
    cfg = Config(sources=scfg, aliases={}, snapshot_path=out / SNAPSHOT_NAME, show_args=set())
    col = Collector(
        cfg, sources, out / SNAPSHOT_NAME, tick_s=args.tick, views_path=out / VIEWS_NAME
    )

    def on_term(signum, frame):
        col.stop()
        os._exit(0)

    signal.signal(signal.SIGTERM, on_term)
    col.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
