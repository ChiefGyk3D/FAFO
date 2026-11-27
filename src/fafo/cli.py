"""Command line: `fafo run` (the collector) and `fafo doctor` (one self-test per source).

Output rules: nothing printed here ever carries a token, a key path, a config value or raw
exception text. Doctor details are the exception class name, plus the message only for the
two exception families whose messages are documented as sanitized (SshError, GhError); every
detail then goes through credential masking, control-character stripping, host aliases and a
length cap.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import fafo

from .collector import Collector
from .config import Config, ConfigError, SourceCfg, load_config
from .ghapi import GhError
from .redact import mask_credentials, sanitize_text
from .registry import Factory, build
from .source import Ctx, Source, SourceResult
from .sshro import SshError

DOCTOR_TIMEOUT_S = 5.0
DETAIL_MAX = 120
EXIT_OK, EXIT_ERROR, EXIT_CONFIG = 0, 1, 2
_STATUSES = ("ok", "absent", "down", "stale")
_SAFE_MESSAGE_ERRORS = (SshError, GhError)


def _safe(cfg: Config, text: object, cap: int = DETAIL_MAX) -> str:
    """One line, masked, stripped of control characters, hostnames aliased, capped."""
    flat = str(text).replace("\n", " ").replace("\r", " ").replace("\t", " ")
    cleaned = cfg.alias(sanitize_text(mask_credentials(flat), 1000))
    return sanitize_text(cleaned, cap)


def add_render_commands(subparsers: Any) -> None:
    """Extension point: registers table, json, sessions and tui (see render/commands.py)."""
    from .render import commands

    commands.add_render_commands(subparsers)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fafo", description="Read-only observer of AI agents.")
    p.add_argument("--version", action="version", version=f"fafo {fafo.__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="{run,doctor,table,json,sessions,tui}")
    run = sub.add_parser("run", help="run the collector until SIGINT or SIGTERM")
    run.add_argument("--config", type=Path, default=None, help="TOML config file")
    run.add_argument("--snapshot", type=Path, default=None, help="snapshot file to write")
    doc = sub.add_parser("doctor", help="run every enabled source once and report")
    doc.add_argument("--config", type=Path, default=None, help="TOML config file")
    add_render_commands(sub)
    return p


# -- run ---------------------------------------------------------------------------------------


def _cmd_run(args: argparse.Namespace, factories: Mapping[str, Factory] | None) -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    cfg = load_config(args.config)
    if args.snapshot is not None:
        cfg = dataclasses.replace(cfg, snapshot_path=args.snapshot.expanduser())
    res = build(cfg, os.environ, factories)
    for name, why in res.skipped.items():
        print(f"fafo: skipping {name}: {_safe(cfg, why)}", file=sys.stderr)
    for name, note in res.notes.items():
        print(f"fafo: {name}: {_safe(cfg, note)}", file=sys.stderr)
    collector = Collector(cfg, res.sources, cfg.snapshot_path)

    def on_signal(signum: int, frame: object) -> None:
        collector.stop()

    old = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        collector.run()
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    return EXIT_OK


# -- doctor ------------------------------------------------------------------------------------


def _error_detail(cfg: Config, exc: BaseException) -> str:
    name = type(exc).__name__
    if isinstance(exc, _SAFE_MESSAGE_ERRORS) and str(exc):
        return _safe(cfg, f"{name}: {exc}")
    return _safe(cfg, name)


def _summary(res: SourceResult) -> str:
    parts = []
    for label, n in (
        ("sessions", len(res.sessions)),
        ("procs", len(res.procs)),
        ("runners", len(res.runners)),
        ("llm", 1 if res.llm is not None else 0),
    ):
        if n:
            parts.append(f"{label}={n}")
    return " ".join(parts) or "no rows"


class _Probe:
    """One source collected once in a daemon worker, abandoned on timeout."""

    def __init__(self, src: Source, scfg: SourceCfg) -> None:
        self.src = src
        self.done = threading.Event()
        self.result: SourceResult | None = None
        self.exc: BaseException | None = None
        ctx = Ctx(cfg=scfg, clock=time.time, log=logging.getLogger("fafo.doctor"))

        def target() -> None:
            try:
                self.result = src.collect(ctx)
            except BaseException as exc:  # noqa: BLE001 - reported, never raised
                self.exc = exc
            finally:
                self.done.set()

        self.thread = threading.Thread(target=target, daemon=True, name=f"fafo-doctor-{src.name}")
        self.thread.start()


def _judge(cfg: Config, probe: _Probe, timed_out: bool) -> tuple[str, str]:
    if timed_out:
        return "stale", f"timeout after {DOCTOR_TIMEOUT_S:g}s"
    if probe.exc is not None:
        return "down", _error_detail(cfg, probe.exc)
    res = probe.result
    if not isinstance(res, SourceResult):
        return "down", "bad result type"
    status = res.status or "ok"
    if status not in _STATUSES:
        return "down", "invalid status from source"
    note = getattr(probe.src, "note", "")
    if status == "ok" and isinstance(note, str) and note:
        return "down", _safe(cfg, note)  # e.g. ssh unreachable: not a pass
    return status, _summary(res)


def _cmd_doctor(args: argparse.Namespace, factories: Mapping[str, Factory] | None) -> int:
    cfg = load_config(args.config)
    res = build(cfg, os.environ, factories)
    lines: list[tuple[str, str, str]] = []
    probes = [_Probe(s, cfg.sources[s.name]) for s in res.sources]
    deadline = time.monotonic() + DOCTOR_TIMEOUT_S
    for p in probes:
        p.done.wait(max(0.0, deadline - time.monotonic()))
        timed_out = not p.done.is_set()
        lines.append((p.src.name, *_judge(cfg, p, timed_out)))
    for name, why in res.skipped.items():
        lines.append((name, "absent", _safe(cfg, why)))
    order = {n: i for i, n in enumerate(cfg.sources)}
    lines.sort(key=lambda t: order.get(t[0], len(order)))
    for name, status, detail in lines:
        print(f"{name} {status} {detail}".rstrip())
    sys.stdout.flush()
    return EXIT_OK if all(status in ("ok", "absent") for _, status, _ in lines) else EXIT_ERROR


# -- entry -------------------------------------------------------------------------------------


def main(argv: list[str] | None = None, *, factories: Mapping[str, Factory] | None = None) -> int:
    """Entry point. `factories` is a test seam that overrides source construction."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            return _cmd_run(args, factories)
        if args.command == "doctor":
            return _cmd_doctor(args, factories)
        handler = getattr(args, "handler", None)
        if handler is not None:
            return handler(args)
        return EXIT_ERROR
    except ConfigError as exc:
        text = sanitize_text(mask_credentials(str(exc).replace("\n", " ")), 300)
        print(f"fafo: config error: {text}", file=sys.stderr)
        return EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001 - last resort: class name only, never the text
        print(f"fafo: error: {type(exc).__name__}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
