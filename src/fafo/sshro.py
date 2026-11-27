"""Read-only ssh helper: a fixed allowlist of remote commands, no shell, one deadline.

`ssh_ro` accepts an operation name, never a command. Each name maps to one fixed remote
command string; no caller data is interpolated into it. The local side is an argv list run
without a shell. ssh never prompts (BatchMode), never trusts a new or changed host key
(StrictHostKeyChecking=yes) and offers only the configured key (IdentitiesOnly). Its stderr
is discarded, so errors carry only a generic reason.
"""

from __future__ import annotations

import os
import re
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .redact import sanitize_text

MAX_OUTPUT = 64 * 1024
DEFAULT_TIMEOUT_S = 6.0
_ALIAS = re.compile(r"[A-Za-z0-9._@-]{1,100}")
_USER = re.compile(r"[A-Za-z0-9._-]{1,64}")

OPS: dict[str, str] = {
    "loadavg": "cat /proc/loadavg",
    "meminfo": "cat /proc/meminfo",
    "df": "df -P -k /",
    "workers": "ps -eo pid,etimes,comm,args --no-headers | grep -E '[R]unner.Worker|[R]unner.Listener'",
}

_BASE_ARGV = (
    "ssh",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=4",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "IdentitiesOnly=yes",
    "-o", "ClearAllForwardings=yes",
    "-o", "ControlMaster=no",
    "-o", "RequestTTY=no",
)  # fmt: skip


class SshError(Exception):
    """Connection, host key, auth, timeout or output problem. Message is short and sanitized."""


@dataclass(frozen=True)
class SshHost:
    alias: str
    key_path: str | None = None
    user: str | None = None


Runner = Callable[[Sequence[str], float, Mapping[str, str]], "tuple[int, bytes]"]


def _target(host: SshHost) -> str:
    alias = host.alias
    if not isinstance(alias, str) or not _ALIAS.fullmatch(alias) or alias.startswith("-"):
        raise ValueError("invalid ssh alias")
    if host.user is None:
        return alias
    if not isinstance(host.user, str) or not _USER.fullmatch(host.user) or host.user[0] == "-":
        raise ValueError("invalid ssh user")
    return f"{host.user}@{alias}"


def build_argv(host: SshHost, op: str) -> list[str]:
    """The local argv for one allowlisted op. Raises ValueError for anything else."""
    if not isinstance(op, str) or op not in OPS:
        raise ValueError("operation not allowed")
    target = _target(host)
    argv = list(_BASE_ARGV)
    if host.key_path is not None:
        if not isinstance(host.key_path, str) or not host.key_path or "\x00" in host.key_path:
            raise ValueError("invalid key path")
        argv += ["-i", host.key_path]
    argv.append(target)
    argv.append(OPS[op])
    return argv


def _env() -> dict[str, str]:
    env = {"LANG": "C"}
    for key in ("PATH", "HOME", "SSH_AUTH_SOCK"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def default_runner(argv: Sequence[str], timeout_s: float, env: Mapping[str, str]) -> tuple[int, bytes]:
    """Run argv; return (exit code, stdout capped at MAX_OUTPUT + 1 bytes).

    One monotonic deadline covers reads and the exit wait. The child's whole process group is
    killed in `finally`. Raises FileNotFoundError, OSError or subprocess.TimeoutExpired.
    """
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=dict(env),
        shell=False,
        start_new_session=True,
        bufsize=0,
    )
    deadline = time.monotonic() + timeout_s
    buf = bytearray()
    try:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        with selectors.DefaultSelector() as sel:
            sel.register(fd, selectors.EVENT_READ)
            while len(buf) <= MAX_OUTPUT:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not sel.select(remaining):
                    raise subprocess.TimeoutExpired(list(argv), timeout_s)
                chunk = os.read(fd, min(65536, MAX_OUTPUT + 1 - len(buf)))
                if not chunk:
                    break
                buf += chunk
        if len(buf) > MAX_OUTPUT:
            return 0, bytes(buf)
        rc = proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        return rc, bytes(buf)
    finally:
        # Kill the group only while the child is still running (not yet reaped), so a reused
        # pid can never receive the signal. After a normal exit nothing is signalled.
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if proc.stdout is not None:
            proc.stdout.close()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _fail(reason: str) -> SshError:
    return SshError(sanitize_text(reason, 200))


def ssh_ro(
    host: SshHost, op: str, timeout_s: float = DEFAULT_TIMEOUT_S, runner: Runner | None = None
) -> str:
    """Run one allowlisted read-only op on `host`; return its stdout as text."""
    argv = build_argv(host, op)  # ValueError before anything runs
    run = runner or default_runner
    try:
        rc, out = run(argv, timeout_s, _env())
    except subprocess.TimeoutExpired:
        raise _fail(f"ssh {op} timed out after {timeout_s:g}s") from None
    except OSError:
        raise _fail("ssh could not be started") from None
    if len(out) > MAX_OUTPUT:
        raise _fail(f"ssh {op} output over {MAX_OUTPUT} bytes")
    if rc == 255:
        raise _fail("ssh connection, host key or auth failure (exit 255)")
    if rc == 1 and op == "workers" and not out:
        return ""  # grep found no runner process: a normal answer
    if rc != 0:
        raise _fail(f"ssh {op} exited with {rc}")
    return out.decode("utf-8", "replace")
