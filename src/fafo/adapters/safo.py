"""SAFO adapter; SHIT is the same class built with another command and field names.

Finding (checked 2026-11-27 against `safo --help` and docs/agents-status.md): SAFO offers
no machine-readable per-session source. `safo agents-status` is human text with no --json and
reads the board over the network; `safo usage --json` and `safo local report --json` carry
usage, not issue or card data. So this adapter reads a documented JSON shape from a
configured command, and reports `absent` when the command is missing or its output does not
match, strictly and all or nothing (exact top-level key, at most 1000 rows, every row an
object with only the five keys, unique non-empty session ids, string or null values). Shape (see tests/fixtures/safo_agents_status.json):

    {"sessions": [{"session_id": "s1", "issue": "owner/repo#12", "card_status": "In progress",
                   "epic": "Epic name", "effort": "M"}]}

`field_names` maps our names (session_id, issue, card_status, epic, effort) to the keys the
command emits. If the command gives no issue for a session, a brief line `issue: owner/repo#N`
in that session's text is used.

The command runs as an argument list (never a shell), with a timeout, a minimal environment,
stdin closed and output capped at 256 KB. After `enrich`, `health` is ok, stale or absent.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence

from fafo.adapters.base import Enrichment, clean

MAX_OUTPUT = 256 * 1024
MAX_DEPTH = 8
MAX_ROWS = 1000
_SESSION_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_ISSUE_LINE = re.compile(r"issue: ([A-Za-z0-9._-]{1,100})/([A-Za-z0-9._-]{1,100})#([0-9]{1,7})")
_DEFAULT_FIELDS = {
    "session_id": "session_id",
    "issue": "issue",
    "card_status": "card_status",
    "epic": "epic",
    "effort": "effort",
}
_VALUE_KEYS = ("issue", "card_status", "epic", "effort")


def parse_issue_line(text: str) -> str | None:
    """Return `owner/repo#N` from the first `issue:` line, or None. First match only."""
    for line in text.split("\n"):
        if line.startswith("issue:"):
            m = _ISSUE_LINE.fullmatch(line)
            return f"{m[1]}/{m[2]}#{m[3]}" if m else None
    return None


def _depth_ok(obj: object) -> bool:
    stack = [(obj, 1)]
    while stack:
        cur, d = stack.pop()
        if d > MAX_DEPTH:
            return False
        if isinstance(cur, dict):
            stack.extend((v, d + 1) for v in cur.values())
        elif isinstance(cur, list):
            stack.extend((v, d + 1) for v in cur)
    return True


def _default_runner(argv: Sequence[str], timeout_s: float, env: Mapping[str, str]) -> bytes:
    """Run argv; return at most MAX_OUTPUT + 1 bytes of stdout.

    One monotonic deadline covers the whole call (reads and exit). On deadline or oversize
    the child's whole process group is killed. Raises FileNotFoundError or
    subprocess.TimeoutExpired.
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
        if len(buf) <= MAX_OUTPUT:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        return bytes(buf)
    finally:
        try:  # before reaping, so the group id cannot have been reused
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if proc.stdout is not None:
            proc.stdout.close()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


class SafoAdapter:
    def __init__(
        self,
        command: Sequence[str],
        timeout_s: float = 5.0,
        field_names: Mapping[str, str] | None = None,
        runner: Callable[[Sequence[str], float, Mapping[str, str]], bytes] | None = None,
        name: str = "safo",
    ) -> None:
        self.name = name
        self.command = list(command)
        self.timeout_s = timeout_s
        self.fields = {**_DEFAULT_FIELDS, **(field_names or {})}
        self._runner = runner or _default_runner
        self.health = "absent"

    def _env(self) -> dict[str, str]:
        env = {"LANG": "C"}
        for key in ("PATH", "HOME"):
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    def _fetch(self) -> list[dict[str, object]] | None:
        try:
            raw = self._runner(self.command, self.timeout_s, self._env())
        except (subprocess.TimeoutExpired, TimeoutError):
            self.health = "stale"
            return None
        except OSError:
            self.health = "absent"
            return None
        if len(raw) > MAX_OUTPUT:
            self.health = "absent"
            return None
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            self.health = "absent"
            return None
        rows = self._validate(data)
        if rows is None:
            self.health = "absent"
            return None
        self.health = "ok"
        return rows

    def _validate(self, data: object) -> list[dict[str, object]] | None:
        """All-or-nothing: any violation of the documented shape rejects the whole output."""
        if not isinstance(data, dict) or set(data) != {"sessions"} or not _depth_ok(data):
            return None
        rows = data["sessions"]
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            return None
        id_key = self.fields["session_id"]
        allowed = {self.fields[k] for k in _DEFAULT_FIELDS}
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or not set(row) <= allowed:
                return None
            sid = row.get(id_key)
            if not isinstance(sid, str) or not _SESSION_ID.fullmatch(sid) or sid in seen:
                return None
            seen.add(sid)
            if any(k != id_key and v is not None and not isinstance(v, str)
                   for k, v in row.items()):
                return None
        return rows

    def enrich(
        self, session_ids: Sequence[str], text_by_session: Mapping[str, str]
    ) -> dict[str, Enrichment]:
        rows = self._fetch()
        if rows is None:
            return {}
        wanted = set(session_ids)
        out: dict[str, Enrichment] = {}
        for row in rows:
            sid = row[self.fields["session_id"]]
            if sid not in wanted:
                continue
            vals = {k: clean(row.get(self.fields[k])) for k in _VALUE_KEYS}
            if vals["issue"] is None:
                vals["issue"] = parse_issue_line(text_by_session.get(sid, ""))
            out[sid] = Enrichment(**vals)
        return out
