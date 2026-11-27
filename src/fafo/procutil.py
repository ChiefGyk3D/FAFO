"""Process-group cleanup for the subprocess runners: kill first, reap second.

The runners start a child in its own session, so its pid is also its process group id. The
group must be killed on every path, including a normal exit, because the child may have left a
grandchild running in the same group. That kill must never happen AFTER the child is reaped:
once `wait` returns, the kernel may hand the pid to a new process that is its own group
leader, and `killpg` would then hit an unrelated group.

The rule used here: an unreaped leader (running, or exited but still a zombie) keeps its pid
and group id reserved, so signalling the group while the leader is unreaped is safe. Therefore
every path does: read output with the deadline, wait for exit WITHOUT reaping
(`wait_exited_unreaped`, waitid with WNOWAIT), kill the group, then reap with a bounded wait
(`kill_and_reap`).

`poll()` is deliberately not used to decide: it reaps an exited child, which would make the
grandchild unreachable. The guard reads `proc.returncode`, which is only set once something
has reaped the child, so an already-reaped Popen never gets a signal and a second call is a
no-op.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

_POLL_S = 0.005


def kill_group_if_running(proc: subprocess.Popen) -> None:
    """SIGKILL the child's process group unless the child was already reaped."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def kill_and_reap(proc: subprocess.Popen, reap_timeout: float = 2.0) -> None:
    """Kill the group (if the child is unreaped), then reap with a bounded wait."""
    kill_group_if_running(proc)
    try:
        proc.wait(timeout=reap_timeout)
    except subprocess.TimeoutExpired:
        pass


def wait_exited_unreaped(proc: subprocess.Popen, timeout: float) -> bool:
    """Wait until the child has exited without reaping it. False on timeout.

    True also when the child was already reaped.
    """
    if proc.returncode is not None:
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    while True:
        try:
            if os.waitid(os.P_PID, proc.pid, flags) is not None:
                return True
        except ChildProcessError:
            return True  # already reaped elsewhere
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_S)
