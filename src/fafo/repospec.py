"""The one rule for an `owner/name` repository string, shared by config and the GitHub client."""

from __future__ import annotations

import re

_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}")


def valid_repo(repo: object) -> bool:
    """True for `owner/name`: two parts of letters, digits, `.`, `_`, `-`; no `.` part and no `..` anywhere."""
    if not isinstance(repo, str) or _REPO.fullmatch(repo) is None:
        return False
    return ".." not in repo and all(part != "." for part in repo.split("/"))
