"""Short, redacted label of what a Claude session is doing, from the tail of its transcript.

The transcript is a private conversation. This module reads at most the last 64 KB of one
regular file the effective user owns, keeps one label of at most 80 characters, and logs
nothing: not a path, not a label, not a line.

Modes: "off" (no label), "activity" (default: tool name plus a minimal object such as the
first word of a command or a file basename, with an age suffix after 120 s) and "prompt" (opt-in: the start of the last
human message, credential-masked).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import unicodedata
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .redact import masked_text, sanitize_text

MODES = ("off", "activity", "prompt")
TAIL_BYTES = 64 * 1024
MAX_LINE = 256 * 1024
MAX_DIRS = 2000
MAX_DEPTH = 32
MAX_LABEL = 80
PROMPT_CHARS = 60
IDLE_AFTER_S = 120.0  # older than this, the label gets an age suffix
MAX_MINUTES = 99
CACHE_MAX = 512
RESCAN_S = 30.0  # minimum clock seconds between full scans for a session with no transcript
ELLIPSIS = "..."

_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)
_WORD_RE = re.compile(r"[A-Za-z0-9._-]{1,20}", re.ASCII)
_AGENT_RE = re.compile(r"[A-Za-z0-9._-]{1,30}", re.ASCII)
_NAME_RE = re.compile(r"[A-Za-z0-9._:-]{1,40}", re.ASCII)
_FILE_TOOLS = {"Read": "file_path", "Edit": "file_path", "Write": "file_path",
               "NotebookEdit": "notebook_path"}
_AGENT_TOOLS = ("Task", "Agent")

# (projects dir, session id, mode) -> (size, mtime_ns, mtime_s, label or None). Bounded.
_cache: OrderedDict[tuple[str, str, str], tuple[int, int, float, str | None]] = OrderedDict()
# (projects dir, session id) -> (project directory name or None, clock time of the scan). Bounded.
_paths: OrderedDict[tuple[str, str], tuple[str | None, float]] = OrderedDict()


def clear_cache() -> None:
    _cache.clear()
    _paths.clear()


class _Chain:
    """An open projects dir -> project dir -> transcript descriptor chain, checked with fstat."""

    def __init__(self, fds: list[int], st: os.stat_result) -> None:
        self.fds = fds
        self.st = st

    @property
    def fd(self) -> int:
        return self.fds[-1]

    def close(self) -> None:
        for d in reversed(self.fds):
            with contextlib.suppress(OSError):
                os.close(d)
        self.fds = []


def _open_chain(projects: Path, dirname: str, fname: str) -> _Chain | None:
    """Open the file by descriptors: no path string is followed after the projects directory.

    The project directory and the file are opened with O_NOFOLLOW relative to their parent, so
    a swapped-in symlink is refused. Every step is fstat-checked: directories and the file are
    owned by the effective user and the file is regular. The projects directory itself is the
    configured root and may be a symlink the owner set up.
    """
    uid = os.geteuid()
    fds: list[int] = []
    try:
        fds.append(os.open(os.fspath(projects), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        fds.append(os.open(dirname, flags, dir_fd=fds[0]))
        fds.append(
            os.open(
                fname,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=fds[1],
            )
        )
        if any(os.fstat(d).st_uid != uid for d in fds):
            raise OSError("not owned by the effective user")
        st = os.fstat(fds[2])
        if not stat.S_ISREG(st.st_mode):
            raise OSError("not a regular file")
        return _Chain(fds, st)
    except OSError:
        for d in reversed(fds):
            with contextlib.suppress(OSError):
                os.close(d)
        return None


def _locate_cached(sid: str, projects: Path, now: float) -> _Chain | None:
    """Open chain for the session's transcript. The cached project directory is tried first
    (descriptor checks only); a directory scan runs only when that fails, and a miss is
    remembered for RESCAN_S so a session without a transcript costs one scan per window."""
    pk = (os.path.abspath(projects), sid)
    fname = f"{sid}.jsonl"
    ent = _paths.get(pk)
    if ent is not None:
        dirname, scanned = ent
        if dirname is not None:
            chain = _open_chain(projects, dirname, fname)
            if chain is not None:
                _paths.move_to_end(pk)
                return chain
        elif now - scanned < RESCAN_S:
            return None
    found = _locate(sid, projects)
    chain = _open_chain(projects, found, fname) if found is not None else None
    _paths[pk] = (found if chain is not None else None, now)
    _paths.move_to_end(pk)
    while len(_paths) > CACHE_MAX:
        _paths.popitem(last=False)
    return chain


def _locate(sid: str, projects: Path) -> str | None:
    """Name of the project directory that appears to hold <sid>.jsonl. Reads no file content;
    the result is only a hint that _open_chain verifies."""
    fname = f"{sid}.jsonl"
    try:
        with os.scandir(projects) as it:
            for n, entry in enumerate(it):
                if n >= MAX_DIRS:
                    return None
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    st = os.lstat(os.path.join(entry.path, fname))
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid():
                    return entry.name
    except OSError:
        return None
    return None


def _read_tail(chain: _Chain) -> bytes | None:
    """Last TAIL_BYTES of the already opened and checked file."""
    fd, st = chain.fd, chain.st
    try:
        start = max(0, st.st_size - TAIL_BYTES)
        os.lseek(fd, start, os.SEEK_SET)
        chunks: list[bytes] = []
        left = TAIL_BYTES
        while left > 0:
            part = os.read(fd, left)
            if not part:
                break
            chunks.append(part)
            left -= len(part)
        data = b"".join(chunks)
        if start > 0:  # the first line is cut mid-way
            _, _, data = data.partition(b"\n")
        return data
    except OSError:
        return None


def _depth_ok(value: Any) -> bool:
    stack = [(value, 1)]
    while stack:
        cur, depth = stack.pop()
        if depth > MAX_DEPTH:
            return False
        if isinstance(cur, dict):
            stack.extend((v, depth + 1) for v in cur.values())
        elif isinstance(cur, list):
            stack.extend((v, depth + 1) for v in cur)
    return True


def _entries_newest_first(data: bytes):
    """Parsed JSON objects from the end backwards; bad, huge or deep lines are skipped."""
    for raw in reversed(data.split(b"\n")):
        if not raw or len(raw) > MAX_LINE:
            continue
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            continue
        if type(doc) is dict and _depth_ok(doc):
            yield doc


def _content(doc: dict) -> Any:
    msg = doc.get("message")
    return msg.get("content") if type(msg) is dict else None


def _basename(value: Any) -> str:
    if type(value) is not str:
        return ""
    return value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _describe(name: Any, inp: Any) -> str:
    """`<Tool>: <short object>`, or the tool name alone. Never more than the rules allow."""
    if type(name) is not str or _NAME_RE.fullmatch(name) is None:
        return ""
    inp = inp if type(inp) is dict else {}
    obj = ""
    if name == "Bash":
        cmd = inp.get("command")
        words = cmd.split(None, 1) if type(cmd) is str else []
        word = _basename(words[0]) if words else ""
        obj = word if _WORD_RE.fullmatch(word) else ""
    elif name in _FILE_TOOLS:
        obj = _basename(inp.get(_FILE_TOOLS[name]))
    elif name in _AGENT_TOOLS:
        kind = inp.get("subagent_type")
        obj = kind if type(kind) is str and _AGENT_RE.fullmatch(kind) else ""
    if obj:
        obj = masked_text(obj, 200)
    return f"{name}: {obj}" if obj else name


def _last_tool(data: bytes) -> str | None:
    for doc in _entries_newest_first(data):
        if doc.get("type") != "assistant":
            continue
        content = _content(doc)
        if type(content) is not list:
            continue
        for block in reversed(content):
            if type(block) is dict and block.get("type") == "tool_use":
                return _describe(block.get("name"), block.get("input"))
    return None


def _human_text(doc: dict) -> str | None:
    if doc.get("type") != "user" or doc.get("isMeta") is True:
        return None
    content = _content(doc)
    if type(content) is str:
        return content
    if type(content) is list:
        texts = [b.get("text") for b in content if type(b) is dict and b.get("type") == "text"]
        texts = [t for t in texts if type(t) is str]
        if texts:
            return " ".join(texts)
    return None  # tool_result-only and unknown shapes are not human messages


_KEYWORDS = frozenset(
    ["password", "passwd", "pwd", "pass", "passphrase", "secret", "token", "apikey", "api-key", "api_key", "key", "credential", "credentials", "bearer", "auth", "authorization", "cookie", "session", "private"]
)
_CONNECTORS = frozenset({"is", "was", "are", "to", "=", ":", "->"})
MAX_HIDDEN = 2
MAX_CONNECTORS = 3
_MASK = "<redacted>"


def _hide(word: str) -> str:
    """Mask of a word that is never longer than the word."""
    return _MASK if len(word) >= len(_MASK) else "*" * len(word)


def mask_phrases(text: str) -> str:
    """Hide the words that follow a credential keyword, in one linear pass over the tokens.

    `password: hunter2` and `my token is abc` keep the keyword and hide up to MAX_HIDDEN
    following words (after optional connectors); `password=hunter2` hides the glued value.
    Best effort and deliberately over-eager: a benign `key binding` loses a word. The result
    is never longer than the input.
    """
    words = text.split()
    out: list[str] = []
    i = 0
    n = len(words)
    while i < n:
        w = words[i]
        i += 1
        out.append(w)
        low = w.lower()
        head, sep, tail = low.partition("=")
        if not sep:
            head, sep, tail = low.partition(":")
        if sep and tail and head in _KEYWORDS:  # glued: keyword=value, keyword:value
            out[-1] = w[: len(head) + 1] + _hide(w[len(head) + 1 :])
            continue
        if low.rstrip(":=,") not in _KEYWORDS:
            continue
        skipped = 0
        while i < n and skipped < MAX_CONNECTORS and words[i].lower() in _CONNECTORS:
            out.append(words[i])
            i += 1
            skipped += 1
        for _ in range(MAX_HIDDEN):
            if i >= n or words[i].lower().rstrip(":=,") in _KEYWORDS:
                break  # a following keyword starts its own window
            out.append(_hide(words[i]))
            i += 1
    return " ".join(out)


def _spaces(text: str) -> str:
    """One space for every whitespace run, before sanitizing removes control characters.

    Newlines, tabs, NBSP, line separators and invisible format characters would otherwise be
    deleted and glue a keyword to its value (`password\\nhunter2`).
    """
    flat = "".join(
        " " if ch.isspace() or unicodedata.category(ch) in ("Zs", "Zl", "Zp", "Cf") else ch
        for ch in text
    )
    return " ".join(flat.split())


def _last_prompt(data: bytes) -> str | None:
    for doc in _entries_newest_first(data):
        text = _human_text(doc)
        if text is None:
            continue
        text = masked_text(_spaces(text), 2000)
        if not text:
            continue
        # Cut first: the phrase pass only ever sees PROMPT_CHARS characters.
        cut = mask_phrases(text[:PROMPT_CHARS])
        return cut + ELLIPSIS if len(text) > PROMPT_CHARS else cut
    return None


def _remember(key: tuple[str, str, str], value: tuple[int, int, float, str | None]) -> None:
    _cache[key] = value
    _cache.move_to_end(key)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)


def _final(text: str, cap: int) -> str:
    return sanitize_text(" ".join(text.split()), cap)


def _age_suffix(age_s: float) -> str:
    """A suffix like ' (5m ago)' once the last tool_use is older than IDLE_AFTER_S. Never 'idle'."""
    if not age_s > IDLE_AFTER_S:
        return ""
    minutes = int(age_s // 60)
    return f" ({minutes}m ago)" if minutes <= MAX_MINUTES else f" (>{MAX_MINUTES}m ago)"


def task_label(session_id: str, projects_dir: Path, mode: str, now: float) -> str:
    """Short redacted label for the session, or "" for any failure. Never raises."""
    try:
        return _task_label(session_id, Path(projects_dir), mode, now)
    except Exception:  # noqa: BLE001 - a label is optional; nothing may escape or be partial
        return ""


def _task_label(sid: str, projects: Path, mode: str, now: float) -> str:
    if mode not in ("activity", "prompt") or type(sid) is not str:
        return ""
    if _ID_RE.fullmatch(sid) is None:
        return ""
    chain = _locate_cached(sid, projects, now)
    if chain is None:
        return ""
    try:
        st = chain.st
        key = (os.path.abspath(projects), sid, mode)
        hit = _cache.get(key)
        if hit is not None and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            _cache.move_to_end(key)
            raw, mtime = hit[3], hit[2]
        else:
            data = _read_tail(chain)
            if data is None:
                return ""
            raw = _last_tool(data) if mode == "activity" else _last_prompt(data)
            mtime = st.st_mtime
            _remember(key, (st.st_size, st.st_mtime_ns, mtime, raw))
    finally:
        chain.close()
    if raw is None:
        return ""
    suffix = _age_suffix(now - mtime) if mode == "activity" else ""
    return _final(raw, MAX_LABEL - len(suffix)) + suffix
