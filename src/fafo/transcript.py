"""Short, redacted label of what a Claude session is doing, from the tail of its transcript.

The transcript is a private conversation. This module reads at most the last 64 KB of one
regular file the effective user owns, keeps one label of at most 80 characters, and logs
nothing: not a path, not a label, not a line.

Modes: "off" (no label), "activity" (default: tool name plus a minimal object such as the
first word of a command or a file basename, with an age suffix after 120 s) and "prompt" (opt-in: the start of the last
human message, credential-masked).
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .redact import mask_credentials, sanitize_text

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
ELLIPSIS = "..."

_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)
_WORD_RE = re.compile(r"[A-Za-z0-9._-]{1,20}", re.ASCII)
_AGENT_RE = re.compile(r"[A-Za-z0-9._-]{1,30}", re.ASCII)
_NAME_RE = re.compile(r"[A-Za-z0-9._:-]{1,40}", re.ASCII)
_FILE_TOOLS = {"Read": "file_path", "Edit": "file_path", "Write": "file_path",
               "NotebookEdit": "notebook_path"}
_AGENT_TOOLS = ("Task", "Agent")

# (session id, mode) -> (size, mtime_ns, mtime_s, label or None). Insertion ordered, bounded.
_cache: OrderedDict[tuple[str, str], tuple[int, int, float, str | None]] = OrderedDict()


def clear_cache() -> None:
    _cache.clear()


def _locate(sid: str, projects: Path) -> tuple[str, os.stat_result] | None:
    """Directory name holding <sid>.jsonl and its lstat result. Reads no file content."""
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
                    return entry.name, st
    except OSError:
        return None
    return None


def _read_tail(projects: Path, dirname: str, sid: str) -> tuple[bytes, os.stat_result] | None:
    """Last TAIL_BYTES of the file, opened without following links, checked on the descriptor."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    pfd = sub = fd = -1
    try:
        pfd = os.open(os.fspath(projects), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        sub = os.open(dirname, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                      dir_fd=pfd)
        fd = os.open(f"{sid}.jsonl", flags, dir_fd=sub)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            return None
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
        return data, st
    except OSError:
        return None
    finally:
        for d in (fd, sub, pfd):
            if d >= 0:
                os.close(d)


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
        obj = mask_credentials(sanitize_text(obj, 200))
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


def _last_prompt(data: bytes) -> str | None:
    for doc in _entries_newest_first(data):
        text = _human_text(doc)
        if text is None:
            continue
        text = mask_credentials(sanitize_text(text[:2000], 2000))
        if not text:
            continue
        return text[:PROMPT_CHARS] + ELLIPSIS if len(text) > PROMPT_CHARS else text
    return None


def _remember(key: tuple[str, str], value: tuple[int, int, float, str | None]) -> None:
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
    found = _locate(sid, projects)
    if found is None:
        return ""
    dirname, st = found
    key = (sid, mode)
    hit = _cache.get(key)
    if hit is not None and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
        _cache.move_to_end(key)
        raw, mtime = hit[3], hit[2]
    else:
        got = _read_tail(projects, dirname, sid)
        if got is None:
            return ""
        data, fst = got
        raw = _last_tool(data) if mode == "activity" else _last_prompt(data)
        mtime = fst.st_mtime
        _remember(key, (fst.st_size, fst.st_mtime_ns, mtime, raw))
    if raw is None:
        return ""
    suffix = _age_suffix(now - mtime) if mode == "activity" else ""
    return _final(raw, MAX_LABEL - len(suffix)) + suffix
