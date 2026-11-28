"""Redaction of process arguments and sanitizing of terminal text.

redact_argv is allowlist-first: only the executable name and arguments the
caller names in show_args survive. Everything else becomes a placeholder.
Retained values are additionally masked when they look like credentials.

Flag arity is explicit. A shown flag is printed alone unless it is also named in
the keyword-only value_flags set: only then is the following token (or the part
after "=") treated as its value and kept, after normalization and credential
masking. For a shown flag not in value_flags, the next token is handled like any
other argument (<arg> unless itself shown) and "--flag=value" becomes <arg>.
sanitize_text removes control characters and terminal escape sequences so
untrusted text cannot drive a terminal.
"""

from __future__ import annotations

import re
import unicodedata

ARGS_MODES = ("allowlist", "safe")
ARG = "<arg>"
SCRIPT = "<script>"
ENV = "<env>"
EXE = "<exe>"
MASK = "<redacted>"

MAX_TOKENS = 64
MAX_REDACTED_LEN = 1000

MAX_TOKEN_LEN = 256
SAFE_MAX_TOKENS = 12

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,254}=")
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
    [a-z][a-z0-9+.-]{{0,255}}://[^/\s@]{{0,256}}@               # url with userinfo
    | \bbearer\s{{1,256}}\S                                    # bearer header value
    | \bbasic\s{{1,256}}[A-Za-z0-9+/=]{{6,300}}                    # basic header value
    | \b(?:gh[pousr]_|github_pat_|sk-|xox[abprs]-|akia|aiza|eyj)[A-Za-z0-9_-]   # known prefixes
    | (?:(?:{_SECRET_SUBSTR})[\w-]{{0,256}}
       |(?<![A-Za-z0-9])(?:{"|".join(sorted(_SECRET_WORDS))}))
      \s{{0,256}}[=:]\s{{0,256}}\S                                 # key=value
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


def redact_argv(
    argv: list[str],
    show_args: set[str],
    *,
    value_flags: frozenset[str] = frozenset(),
    mode: str = "allowlist",
) -> str:
    """Render argv for display. Keeps the executable basename and shown args only.

    mode "safe" additionally shows conservative, fixed-shape parts of the rest (see
    _safe_tail); the default "allowlist" shows nothing beyond show_args.
    """
    if mode not in ARGS_MODES:
        raise ValueError("mode must be 'allowlist' or 'safe'")
    out: list[str] = []
    items = list(argv[: MAX_TOKENS * 4])
    shown = {_normalize(a) for a in show_args if isinstance(a, str) and len(a) <= MAX_TOKEN_LEN}
    valued = {_normalize(a) for a in value_flags if isinstance(a, str) and len(a) <= MAX_TOKEN_LEN}
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
        exe_ok = _SAFE_EXE.match(base) and not _is_credential(base)
        out.append(base if exe_ok else EXE)
        interp = base if exe_ok and (base in _INTERPRETERS or _PYTHON.match(base)) else None
        i += 1
    else:
        interp = None

    if mode == "safe":
        _safe_tail(items, i, shown, valued, interp, out)
        if len(out) > SAFE_MAX_TOKENS:
            out = [*out[:SAFE_MAX_TOKENS], "..."]
        return sanitize_text(" ".join(out), MAX_REDACTED_LEN)

    while i < n:
        tok = norm(i)
        i += 1
        if tok is None:
            out.append(ARG)
            continue
        if tok in shown:
            nxt_raw = items[i] if i < n else None
            if tok in valued and isinstance(nxt_raw, str):
                nxt = norm(i)
                if nxt is None or not nxt.startswith("-"):
                    out.append(_keep(tok))
                    out.append(_keep(nxt, tok) if nxt is not None else MASK)
                    i += 1
                    continue
            out.append(_keep(tok))
            continue
        if tok.startswith("-") and "=" in tok:
            name, _, value = tok.partition("=")
            if name in shown and name in valued:
                out.append(f"{_keep(name)}={_keep(value, name)}")
                continue
        out.append(ARG)

    if len(out) > MAX_TOKENS:
        out = [*out[:MAX_TOKENS], "..."]
    return sanitize_text(" ".join(out), MAX_REDACTED_LEN)


# ---- Safe mode (args_mode = "safe") ----

_INTERPRETERS = frozenset(
    {"python", "python3", "node", "nodejs", "bun", "bash", "sh", "zsh", "ruby", "perl", "uv",
     "npx", "pipx"}
)
_PYTHON = re.compile(r"^python3\.[0-9]{1,2}$")
_FLAG_NAME = re.compile(r"^-{1,2}[A-Za-z][A-Za-z0-9-]{0,30}$")
_WORD = re.compile(r"^[A-Za-z0-9_.+-]{1,40}$")
_SCRIPT_WORD = re.compile(r"^[A-Za-z0-9_.+-]{1,60}$")
_PATH_CHARS = re.compile(r"^[A-Za-z0-9_.+/~-]+$")
# Known credential prefixes anywhere in a word (the main pattern needs a word boundary).
_EMBEDDED_PREFIX = re.compile(
    r"(?i)(?:gh[pousr]_|github_pat_|sk-|xox[abprs]-|akia|aiza|eyj)[A-Za-z0-9_-]{10,}"
)
_SWITCH_LETTERS = frozenset("vxlarfhqstzind")
_CODE_CLUSTER = re.compile(r"^(-[abdf-zA-Z]{0,4}[ce])")
_HEADER_WORDS = frozenset({"bearer", "basic", "authorization"})
_SINGLE_DASH_MAX = 5  # letters after a single dash; longer is likely an option with a glued value


def _secretish(word: str) -> bool:
    """Credential-shaped by the redactor's patterns, or long and digit-bearing (hash, base64)."""
    if _is_credential(word) or _EMBEDDED_PREFIX.search(word):
        return True
    return len(word) >= 16 and any(c.isdigit() for c in word)


def _flag_ok(name: str) -> bool:
    """A flag name that may be shown: fixed shape, not secret-named, not credential-shaped."""
    if not _FLAG_NAME.match(name):
        return False
    if not name.startswith("--"):
        if len(name) - 1 > _SINGLE_DASH_MAX:
            return False
        if len(name) > 2 and name[1] in "pP":  # -psecret: a glued password
            return False
        # -mfoo, -uroot: a letter outside the combinable switches may start an attached value.
        if any(c not in _SWITCH_LETTERS for c in name[2:]):
            return False
    return not _secret_name(name.lstrip("-")) and not _secretish(name)


def _safe_word(tok: str, pattern: re.Pattern[str], *, any_path: bool) -> str:
    """The safe display of a positional: a bare word, or the basename of a path, else ARG."""
    if "/" in tok:
        if not _PATH_CHARS.match(tok) or not (any_path or tok.startswith(("/", "./", "~"))):
            return ARG
        if _CREDENTIAL_VALUE.search(tok):
            return ARG
        word = tok.rsplit("/", 1)[-1]
    else:
        word = tok
    if not pattern.match(word) or _secretish(word):
        return ARG
    return word


def _code_flag(tok: str) -> str | None:
    """The displayable part of an interpreter code flag, or None when tok is not one.

    -E and --eval show as they are. A single-dash cluster is a code flag when a c or e
    comes within its first five letters; only the canonical "-c" or "-e" is returned,
    never the letters before it (they can be an attached option value) or after it
    ("-lcpass" gives "-c").
    """
    if tok in ("--eval", "-E"):
        return tok
    m = _CODE_CLUSTER.match(tok)
    if not m:
        return None
    return "-" + m.group(1)[-1]  # only the canonical -c or -e, never letters before it


def _safe_tail(
    items: list[str],
    i: int,
    shown: set[str],
    valued: set[str],
    interp: str | None,
    out: list[str],
) -> None:
    """Append the safe-mode rendering of items[i:] to out (stops once out exceeds the cap)."""
    n = len(items)
    prev_secret = False  # the previous token was a secret-named flag: this one is its value
    after_flag = False  # the previous token was a flag: a plain word now is its value
    script_pending = interp is not None  # first non-option argument of an interpreter
    python = interp is not None and interp.startswith("python")

    def norm(idx: int) -> str | None:
        tok = items[idx]
        if not isinstance(tok, str) or len(tok) > MAX_TOKEN_LEN:
            return None
        return _normalize(tok)

    while i < n and len(out) <= SAFE_MAX_TOKENS:
        tok = norm(i)
        i += 1
        if tok is None:
            out.append(ARG)
            prev_secret = after_flag = False
            continue
        prev, prev_secret = prev_secret, False
        was_flag, after_flag = after_flag, False
        if tok in shown:
            if tok in valued and i < n and isinstance(items[i], str):
                nxt = norm(i)
                if nxt is None or not nxt.startswith("-"):
                    out.append(_keep(tok))
                    out.append(_keep(nxt, tok) if nxt is not None else MASK)
                    i += 1
                    continue
            out.append(_keep(tok))
            after_flag = tok.startswith("-")
            continue
        if tok == "--":  # option terminator: no value follows, later words are ordinary
            out.append(tok)
            continue
        if tok.startswith("-"):
            name, eq, value = tok.partition("=")
            if eq and name in shown and name in valued:
                out.append(f"{_keep(name)}={_keep(value, name)}")
            elif interp is not None and (code := _code_flag(name)) is not None:
                out.append(code)
                out.append(SCRIPT)
                return
            elif python and tok == "-m":
                out.append(tok)
                script_pending = False
                if i < n:
                    mod = norm(i)
                    i += 1
                    out.append(ARG if mod is None else _safe_word(mod, _SCRIPT_WORD, any_path=False))
            elif eq:
                out.append(f"{name}={ARG}" if _flag_ok(name) else ARG)
            else:
                out.append(tok if _flag_ok(tok) else ARG)
                prev_secret = _secret_name(tok.lstrip("-"))
                after_flag = True
            continue
        if tok.lower().rstrip(":") in _HEADER_WORDS:
            out.append(ARG)
            prev_secret = True
            continue
        if prev:
            out.append(ARG)
            script_pending = False
            continue
        if was_flag:  # a value of a flag that is not in value_flags or show_args
            out.append(ARG)
        elif script_pending:
            script_pending = False
            out.append(_safe_word(tok, _SCRIPT_WORD, any_path=True))
        else:
            out.append(_safe_word(tok, _WORD, any_path=False))


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


def mask_credentials(text: str) -> str:
    """Mask credential-looking whitespace-separated words in already-rendered text.

    For sources that hand over a finished command line: the argv is gone, so this is
    defense in depth, not allowlist redaction. Input is capped at 4 * MAX_REDACTED_LEN
    characters and every word is checked by the same bounded patterns as redact_argv.
    Text past the cap is ignored. A word that the cap cuts may continue past it, so that
    word is masked whole, never kept as a fragment. The cut is taken on the raw text, so
    controls before it can leave a short piece; that piece is the cut word and is masked.
    """
    if not isinstance(text, str):
        return ""
    head = text[: MAX_REDACTED_LEN * 4]
    words = _strip(head).split()
    cut_word = len(words) - 1 if len(text) > len(head) and words else -1
    out: list[str] = []
    mask_next = False
    for index, raw in enumerate(words[: MAX_TOKENS * 16]):
        word = _normalize(raw)
        is_flag = word.startswith("-")
        if (
            index == cut_word
            or (mask_next and not is_flag)
            or len(word) > MAX_TOKEN_LEN
            or _is_credential(word)
        ):
            out.append(MASK)
        else:
            out.append(word)
        # The word after a secret-named flag ("--token x", "-p x") or a header scheme is a value.
        mask_next = word.lower().rstrip(":") in ("bearer", "basic", "authorization") or (
            is_flag and "=" not in word and _secret_name(word.lstrip("-"))
        )
    return " ".join(out)


def masked_text(text: str, cap: int, bound: int = 64 * 1024) -> str:
    """Normalize, then mask credentials, then cut to `cap`.

    Controls are removed first, over the whole input up to `bound` characters (the size of
    a job file), so that a cut never lands before them and a run is judged as the reader
    sees it. Cutting before masking would leave a credential prefix that the masker cannot
    recognize. Callers that display text cut by `cap` should use this, not sanitize_text
    after mask_credentials on a pre-cut string.
    """
    return sanitize_text(mask_credentials(sanitize_text(text, bound)), cap)
