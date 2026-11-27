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

MAX_TOKEN_LEN = 256

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}=")
_SAFE_EXE = re.compile(r"^[A-Za-z0-9_.+-]{1,256}$")

# Secret-like names. Long names match as substrings; short ones only as whole words
# so that "side" or "inside" are not masked by "sid".
_SECRET_SUBSTR = (
    "token|secret|pass|pwd|auth|bearer|cred|key|private|cookie|session|phrase|jwt|signature"
)
_SECRET_WORDS = frozenset({"sid", "pw", "p", "pin", "otp", "sig", "pat"})
_SECRET_NAME = re.compile(_SECRET_SUBSTR)
_WORD_SPLIT = re.compile(r"[^a-z0-9]+")
_CREDENTIAL_VALUE = re.compile(
    rf"""(?ix)
    [a-z][a-z0-9+.-]{{0,31}}://[^/\s@]{{0,256}}@               # url with userinfo
    | \bbearer\s{{1,16}}\S                                    # bearer header value
    | \bbasic\s{{1,16}}[A-Za-z0-9+/=]{{6,}}                    # basic header value
    | \b(?:gh[pousr]_|github_pat_|sk-|xox[abprs]-|akia|aiza|eyj)[A-Za-z0-9_-]   # known prefixes
    | (?:[\w-]{{0,64}}(?:{_SECRET_SUBSTR})[\w-]{{0,64}}
       |(?<![A-Za-z0-9])(?:{"|".join(sorted(_SECRET_WORDS))}))
      \s{{0,8}}[=:]\s{{0,8}}\S                                 # key=value
    """
)
_LONG_RUN = re.compile(r"[A-Za-z0-9+/=_-]{40,300}")
_PATH_SEGMENT = re.compile(r"^[\w.@+-]{0,39}$")


def _normalize(s: str) -> str:
    """The exact form that is decided on and emitted: stripped, NFKC, stripped again."""
    s = _strip(s)
    s = unicodedata.normalize("NFKC", s)
    return _strip(s)


def _secret_name(name: str) -> bool:
    n = name.lower()
    if _SECRET_NAME.search(n):
        return True
    return any(w in _SECRET_WORDS for w in _WORD_SPLIT.split(n))


def _looks_like_path(value: str) -> bool:
    """A plain path only: no credentials, assignments or spaces."""
    if not value.startswith(("/", "./", "../", "~")):
        return False
    if any(c in value for c in "=: \t"):
        return False
    return all(_PATH_SEGMENT.match(seg) for seg in value.split("/"))


def _is_credential(value: str) -> bool:
    if _CREDENTIAL_VALUE.search(value):
        return True
    if _looks_like_path(value):
        return False
    return _LONG_RUN.search(value) is not None


def _keep(value: str, name: str | None = None) -> str:
    """Return a retained (already normalized) value, masked when it could be a credential."""
    if len(value) > MAX_TOKEN_LEN:
        return MASK
    if name is not None and _secret_name(name):
        return MASK
    if _is_credential(value):
        return MASK
    return value


def redact_argv(argv: list[str], show_args: set[str]) -> str:
    """Render argv for display. Keeps the executable basename and shown args only."""
    out: list[str] = []
    items = list(argv[: MAX_TOKENS * 4])
    shown = {_normalize(a) for a in show_args if isinstance(a, str) and len(a) <= MAX_TOKEN_LEN}
    i = 0
    n = len(items)

    def norm(idx: int) -> str | None:
        """Normalized token, or None if not a str or over the length cap (no regex runs)."""
        tok = items[idx]
        if not isinstance(tok, str) or len(tok) > MAX_TOKEN_LEN:
            return None
        return _normalize(tok)

    # Leading VAR=value words are environment assignments: never shown.
    while i < n:
        raw = items[i]
        if not isinstance(raw, str):
            break
        cand = norm(i)
        if cand is None:
            if _ENV_ASSIGN.match(raw):
                out.append(ENV)
                i += 1
                continue
            break
        if not _ENV_ASSIGN.match(cand):
            break
        out.append(ENV)
        i += 1
    if i < n:
        exe = norm(i)
        base = exe.rsplit("/", 1)[-1] if exe is not None else ""
        out.append(base if _SAFE_EXE.match(base) and not _is_credential(base) else EXE)
        i += 1

    while i < n:
        tok = norm(i)
        i += 1
        if tok is None:
            out.append(ARG)
            continue
        if tok in shown:
            nxt = norm(i) if i < n else None
            nxt_raw = items[i] if i < n else None
            if tok.startswith("-") and isinstance(nxt_raw, str) and not (
                nxt is not None and nxt.startswith("-")
            ):
                out.append(_keep(tok))
                out.append(_keep(nxt, tok) if nxt is not None else MASK)
                i += 1
            else:
                out.append(_keep(tok))
            continue
        if tok.startswith("-") and "=" in tok:
            name, _, value = tok.partition("=")
            if name in shown:
                out.append(f"{_keep(name)}={_keep(value, name)}")
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


def _strip(s: str) -> str:
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
    return "".join(kept)


def sanitize_text(s: str, max_len: int = 200) -> str:
    """Strip control chars and terminal escape sequences, then truncate."""
    if max_len <= 0:
        return ""
    return _strip(s)[:max_len]
