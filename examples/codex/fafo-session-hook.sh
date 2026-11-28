#!/bin/sh
# FAFO: Codex SessionStart hook. Lets `fafo tui --session auto` show "you are here" for Codex.
#
# Reads the hook JSON on stdin (at most 64 KB), keeps ONLY session_id (a UUID, 8-4-4-4-12 hex
# digits; anything else is ignored) and writes <state dir>/codex-here/<codex pid>.json with {session_id, ppid,
# started_at}. It never reads transcript_path, prints nothing, writes nothing else, and always
# exits 0 so it can never block a Codex session.
#
# State dir (the same private directory as the FAFO snapshot): the first argument if given, else
# $XDG_RUNTIME_DIR/fafo, else ~/.local/state/fafo. Created 0700; the file is 0600, written
# atomically. The state dir and codex-here are opened without following symlinks and must be
# directories owned by you with no group or other access; otherwise nothing is written. The
# directories ABOVE the state dir are resolved once with realpath (so a symlinked home still works)
# and are trusted: pass only a state path you control. Files for dead sessions are never removed by FAFO; clean up with, for example:
#   find "${XDG_RUNTIME_DIR:-$HOME/.local/state}/fafo/codex-here" -name '*.json' -mtime +7 -delete
#
# Install: see "Using FAFO with Codex" in the README.

trap 'exit 0' EXIT
command -v python3 >/dev/null 2>&1 || exit 0

PY=$(cat <<'PYEOF'
import json
import os
import re
import stat
import sys
import time

MAX_STDIN = 65536
SID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def open_dir(parent_fd, name, check):
    """Open (creating 0700 if missing) one directory below parent_fd, never following a symlink.

    With check set, the opened descriptor must be a directory owned by the effective user with
    no group or other permission bits; otherwise it is refused (ValueError)."""
    created = False
    try:
        fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise ValueError("not a directory")
        if created:
            os.fchmod(fd, 0o700)
            st = os.fstat(fd)
        if check and (st.st_uid != os.geteuid() or st.st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
            raise ValueError("directory is not private")
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_here(base):
    """Descriptor of <base>/codex-here. The parents of base are resolved once (they belong to the
    system); base and codex-here are opened without following links and must be private."""
    base = os.path.normpath(base)
    parts = [p for p in os.path.realpath(os.path.dirname(base)).split("/") if p]
    parts.append(os.path.basename(base))
    parts.append("codex-here")
    last = len(parts) - 1
    fd = os.open("/", DIR_FLAGS)
    try:
        for i, name in enumerate(parts):
            nxt = open_dir(fd, name, check=i >= last - 1)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def main():
    raw = sys.stdin.buffer.read(MAX_STDIN + 1)
    if len(raw) > MAX_STDIN:
        return
    doc = json.loads(raw)
    if type(doc) is not dict:
        return
    sid = doc.get("session_id")
    if type(sid) is not str or not SID.fullmatch(sid):
        return
    if len(sys.argv) > 1 and sys.argv[1]:
        base = sys.argv[1]
    elif os.environ.get("XDG_RUNTIME_DIR"):
        base = os.path.join(os.environ["XDG_RUNTIME_DIR"], "fafo")
    else:
        base = os.path.join(os.path.expanduser("~"), ".local", "state", "fafo")
    if not os.path.isabs(base) or os.path.normpath(base) == "/":
        return
    ppid = os.getppid()
    body = json.dumps({"session_id": sid, "ppid": ppid, "started_at": time.time()}).encode()
    dfd = open_here(base)
    tmp = ".%d.%s.tmp" % (ppid, os.urandom(8).hex())
    try:
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
            dir_fd=dfd,
        )
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, "%d.json" % ppid, src_dir_fd=dfd, dst_dir_fd=dfd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        raise
    finally:
        os.close(dfd)


try:
    main()
except BaseException:  # never fail the hook
    pass
PYEOF
)

# exec keeps the parent pid: python's parent is then the process that started this hook (Codex).
exec python3 -I -c "$PY" "$@" 2>/dev/null
