"""Project attribution: which repository does a session belong to.

Precedence: adapter data, then the most common git repo among child process
cwds, then the session cwd's repo, then the cwd's base name. The result is a
repository NAME only, never a URL, user or credential. Git is run on
untrusted directories, so it only ever gets an argument list, a minimal
environment, a hard timeout and a capped output.
"""

from __future__ import annotations

import os
import re
import select
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Protocol
from urllib.parse import urlsplit

from fafo.procutil import kill_and_reap, wait_exited_unreaped

GIT_TIMEOUT_S = 2.0
CACHE_TTL_S = 30.0
MAX_OUTPUT = 4096
MAX_NAME = 100

_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")


class GitRunner(Protocol):
    def run(self, args: list[str], cwd: str, timeout: float) -> str | None: ...


class SubprocessGit:
    """Default runner: `git -C <cwd> <args>`, no shell, bounded time and output."""

    def __init__(self, git_bin: str = "git") -> None:
        self._git_bin = git_bin

    def run(self, args: list[str], cwd: str, timeout: float) -> str | None:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/"),
            "LANG": "C",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        argv = [self._git_bin, "-C", cwd, *args]
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                shell=False,
                start_new_session=True,
            )
        except (OSError, ValueError):
            return None
        try:
            return _read_capped(proc, timeout)
        finally:
            kill_and_reap(proc, 1.0)  # kill the group before the reap, never after
            if proc.stdout is not None:
                proc.stdout.close()


def _read_capped(proc: subprocess.Popen[bytes], timeout: float) -> str | None:
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            return None
        chunk = os.read(fd, MAX_OUTPUT + 1 - len(buf))
        if not chunk:
            break
        buf += chunk
        if len(buf) > MAX_OUTPUT:
            return None
    if not wait_exited_unreaped(proc, max(deadline - time.monotonic(), 0.01)):
        return None
    kill_and_reap(proc, 1.0)  # exited: kill any grandchild, then reap
    code = proc.returncode
    if code is None or code != 0:
        return None
    return buf.decode("utf-8", errors="replace").strip()


def _clean(name: str) -> str:
    """Drop control characters (C0, DEL, C1) and cap the length."""
    out = "".join(c for c in name if not (ord(c) < 32 or 127 <= ord(c) <= 159))
    return out[:MAX_NAME]


def _has_control(text: str) -> bool:
    return any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in text)


def _name_from_remote(url: str) -> str | None:
    """Repository name from a remote URL, or None if it cannot be parsed safely.

    Only the path of the URL is used; userinfo, host, query and fragment are
    never looked at, so a credential cannot reach the result.
    """
    url = url.strip()
    if not url or _has_control(url):
        return None
    if "://" in url:
        try:
            path = urlsplit(url).path
        except ValueError:
            return None
    else:
        rest = url.rpartition("@")[2]
        _host, sep, path = rest.partition(":")
        if not sep:
            path = rest  # a plain local path
        path = re.split(r"[?#]", path, maxsplit=1)[0]
    segment = path.rstrip("/").rsplit("/", 1)[-1]
    segment = segment.removesuffix(".git")
    if _SAFE_NAME.fullmatch(segment) is None or segment in {".", ".."}:
        return None
    return segment


def _usable_dir(path: str | None) -> bool:
    if not path or not os.path.isabs(path):
        return False
    try:
        return os.path.isdir(path)
    except (OSError, ValueError):
        return False


_cache: dict[tuple[int, str], tuple[float, tuple[str, str] | None]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _lookup(
    directory: str, git: GitRunner, now: Callable[[], float]
) -> tuple[str, str] | None:
    """(toplevel, repo name) for a directory, or None if not a repo. Cached 30 s."""
    if not _usable_dir(directory):
        return None
    key = (id(git), directory)
    t = now()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and 0 <= t - hit[0] < CACHE_TTL_S:
            return hit[1]
    result = _resolve(directory, git)
    with _cache_lock:
        _cache[key] = (t, result)
    return result


def _resolve(directory: str, git: GitRunner) -> tuple[str, str] | None:
    top = git.run(["rev-parse", "--show-toplevel"], directory, GIT_TIMEOUT_S)
    if not top or "\n" in top or not os.path.isabs(top):
        return None
    name = None
    remote = git.run(["config", "--local", "--get", "remote.origin.url"], directory, GIT_TIMEOUT_S)
    if remote:
        name = _name_from_remote(remote)
    if name is None:
        name = _clean(os.path.basename(top.rstrip("/")))
    if not name:
        return None
    return top, name


def _adapter_name(value: str) -> str | None:
    """Safe project name from adapter data, or None if it must be ignored."""
    if re.search(r"[/:@\s]", value):
        return _name_from_remote(value)
    return value if _SAFE_NAME.fullmatch(value) else None


def attribute(
    session_cwd: str | None,
    child_cwds: Sequence[str],
    adapter_project: str | None = None,
    *,
    git: GitRunner | None = None,
    now: Callable[[], float] = time.monotonic,
) -> tuple[str, str]:
    """Return (project, rule) for a session."""
    if adapter_project:
        cleaned = _adapter_name(adapter_project)
        if cleaned:
            return cleaned, "adapter"
    runner: GitRunner = git if git is not None else _DEFAULT_GIT

    counts: Counter[str] = Counter()
    names: dict[str, str] = {}
    for child in child_cwds:
        found = _lookup(child, runner, now)
        if found is not None:
            counts[found[0]] += 1
            names.setdefault(found[0], found[1])
    if counts:
        top = counts.most_common(1)[0][0]  # ties keep first-seen order
        return names[top], "children-git"

    if session_cwd is not None:
        found = _lookup(session_cwd, runner, now)
        if found is not None:
            return found[1], "cwd-git"
        base = _clean(os.path.basename(session_cwd.rstrip("/")))
        if base:
            return base, "cwd-name"
    return "unknown", "cwd-name"


_DEFAULT_GIT: GitRunner = SubprocessGit()
