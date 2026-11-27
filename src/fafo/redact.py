"""Redaction of process arguments and sanitizing of terminal text.

redact_argv is allowlist-first: only the executable name and arguments the
caller names in show_args survive. Everything else becomes a placeholder.
Retained values are additionally masked when they look like credentials.
sanitize_text removes control characters and terminal escape sequences so
untrusted text cannot drive a terminal.
"""

from __future__ import annotations

import re
import unicodedata

ARG = "<arg>"
ENV = "<env>"
EXE = "<exe>"
MASK = "<redacted>"

MAX_TOKENS = 64
MAX_REDACTED_LEN = 1000

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SAFE_EXE = re.compile(r"^[A-Za-z0-9_.+-]+$")
_SECRET_NAME = re.compile(
    r"(?i)(token|secret|passw|pwd|auth|bearer|cred|api[-_]?key|private|cookie|session)"
)
_CREDENTIAL_VALUE = re.compile(
    r"""(?ix)
    [a-z][a-z0-9+.-]*://[^/\s@]*@                              # url with userinfo
    | \bbearer\s+\S                                            # bearer header value
    | \bbasic\s+[A-Za-z0-9+/=]{6,}                             # basic header value
    | \b(?:gh[pousr]_|github_pat_|sk-|xox[abprs]-|akia|aiza|eyj)[A-Za-z0-9_-]   # known prefixes
    | (?:token|secret|passw\w*|pwd|auth\w*|cred\w*|api[-_]?key|key)[\w-]*\s*[=:]\s*\S  # key=value
    """
)
_LONG_RUN = re.compile(r"[A-Za-z0-9+/=_-]{40,}")
_PATH_SEGMENT = re.compile(r"^[\w.@+-]*$")


def _looks_like_path(value: str) -> bool:
    if not value.startswith(("/", "./", "../", "~")):
        return False
    return all(_PATH_SEGMENT.match(seg) and len(seg) < 40 for seg in value.split("/"))


def _is_credential(value: str) -> bool:
    if _CREDENTIAL_VALUE.search(value):
        return True
    if _looks_like_path(value):
        return False
    return _LONG_RUN.search(value) is not None


def _keep(value: str, name: str | None = None) -> str:
    """Return a retained value, masked when it could be a credential."""
    if name is not None and _SECRET_NAME.search(name):
        return MASK
    if _is_credential(value):
        return MASK
    return sanitize_text(value, 200)


def redact_argv(argv: list[str], show_args: set[str]) -> str:
    """Render argv for display. Keeps the executable basename and shown args only."""
    out: list[str] = []
    items = list(argv[: MAX_TOKENS * 4])
    i = 0
    n = len(items)

    # Leading VAR=value words are environment assignments: never shown.
    while i < n and isinstance(items[i], str) and _ENV_ASSIGN.match(items[i]):
        out.append(ENV)
        i += 1
    if i < n:
        exe = items[i]
        base = exe.rsplit("/", 1)[-1] if isinstance(exe, str) else ""
        out.append(base if _SAFE_EXE.match(base) and not _is_credential(base) else EXE)
        i += 1

    while i < n:
        tok = items[i]
        i += 1
        if not isinstance(tok, str):
            out.append(ARG)
            continue
        if tok in show_args:
            nxt = items[i] if i < n else None
            if tok.startswith("-") and isinstance(nxt, str) and not nxt.startswith("-"):
                out.append(_keep(tok))
                out.append(_keep(nxt, tok))
                i += 1
            else:
                out.append(_keep(tok))
            continue
        if tok.startswith("-") and "=" in tok:
            name, _, value = tok.partition("=")
            if name in show_args:
                out.append(f"{sanitize_text(name, 200)}={_keep(value, name)}")
                continue
        out.append(ARG)

    if len(out) > MAX_TOKENS:
        out = [*out[:MAX_TOKENS], "..."]
    return sanitize_text(" ".join(out), MAX_REDACTED_LEN)


_ESC_STRING = re.compile(
    "(?:\x1b[\\]PX^_]|[\x90\x98\x9d\x9e\x9f]).*?(?:\x07|\x1b\\\\|\x9c|\\Z)", re.DOTALL
)
_CSI = re.compile("(?:\x1b\\[|\x9b)[0-?]*[ -/]*[@-~]?")
_ESC_OTHER = re.compile("\x1b[\\s\\S]?")


def sanitize_text(s: str, max_len: int = 200) -> str:
    """Strip control chars and terminal escape sequences, then truncate."""
    if max_len <= 0:
        return ""
    s = _ESC_STRING.sub("", s)
    s = _CSI.sub("", s)
    s = _ESC_OTHER.sub("", s)
    kept = []
    for ch in s:
        cat = unicodedata.category(ch)
        # Cc: C0, DEL, C1. Cf: bidi and zero-width. Cs: lone surrogates. Zl/Zp: line seps.
        if cat in ("Cc", "Cf", "Cs", "Zl", "Zp"):
            continue
        kept.append(ch)
    return "".join(kept)[:max_len]
