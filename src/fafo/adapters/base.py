"""Adapter protocol and the enrichment row. Plain data, no I/O."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

MAX_FIELD = 120
_CONTROL = re.compile("[\u0000-\u001f\u007f-\u009f]")


def clean(value: object) -> str | None:
    """Strip control characters and cap at 120 chars; non-strings and empties become None."""
    if not isinstance(value, str):
        return None
    out = _CONTROL.sub("", value[: MAX_FIELD * 4])[:MAX_FIELD].strip()
    return out or None


@dataclass(frozen=True)
class Enrichment:
    issue: str | None = None
    card_status: str | None = None
    epic: str | None = None
    effort: str | None = None

    def __post_init__(self) -> None:
        for name in ("issue", "card_status", "epic", "effort"):
            object.__setattr__(self, name, clean(getattr(self, name)))


class Adapter(Protocol):
    name: str

    def enrich(
        self, session_ids: Sequence[str], text_by_session: Mapping[str, str]
    ) -> dict[str, Enrichment]: ...
