#!/bin/sh
# FAFO: Codex SessionStart hook. Lets `fafo tui --session auto` show "you are here" for Codex.
#
# Reads the hook JSON on stdin (at most 64 KB), keeps ONLY session_id (A-Za-z0-9_-, 1-128
# characters) and writes <state dir>/codex-here/<codex pid>.json with {session_id, ppid,
# started_at}. It never reads transcript_path, prints nothing, writes nothing else, and always
# exits 0 so it can never block a Codex session.
#
# State dir (the same private directory as the FAFO snapshot): the first argument if given, else
# $XDG_RUNTIME_DIR/fafo, else ~/.local/state/fafo. Created 0700; the file is 0600, written
# atomically. Files for dead sessions are never removed by FAFO; clean up with, for example:
#   find "${XDG_RUNTIME_DIR:-$HOME/.local/state}/fafo/codex-here" -name '*.json' -mtime +7 -delete
#
# Install: see "Using FAFO with Codex" in the README.

trap 'exit 0' EXIT
command -v python3 >/dev/null 2>&1 || exit 0

PY=$(cat <<'PYEOF'
import json
import os
import re
import sys
import tempfile
import time

MAX_STDIN = 65536
SID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def private_dirs(path):
    missing = []
    cur = path
    while not os.path.exists(cur) and cur != os.path.dirname(cur):
        missing.append(cur)
        cur = os.path.dirname(cur)
    for d in reversed(missing):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            continue
        os.chmod(d, 0o700)


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
    if not os.path.isabs(base):
        return
    here = os.path.join(base, "codex-here")
    private_dirs(here)
    ppid = os.getppid()
    body = json.dumps({"session_id": sid, "ppid": ppid, "started_at": time.time()}).encode()
    fd, tmp = tempfile.mkstemp(dir=here, prefix=".", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, os.path.join(here, "%d.json" % ppid))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


try:
    main()
except BaseException:  # never fail the hook
    pass
PYEOF
)

# exec keeps the parent pid: python's parent is then the process that started this hook (Codex).
exec python3 -I -c "$PY" "$@" 2>/dev/null
