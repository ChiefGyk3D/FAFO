"""Gemini source: process tree only, capability `process-only`.

Only the process rows are reported. There is no task, session or transcript
detail, and none is invented.
"""

from __future__ import annotations

from ..proccache import ProcCache
from ..source import Ctx, SourceResult
from .codex import VALUE_FLAGS, Lister, collect_procs, default_cache, default_lister, runs_cli

CAPABILITY = "process-only"


def is_gemini(argv: tuple[str, ...]) -> bool:
    return runs_cli(argv, "gemini")


class GeminiSource:
    name = "gemini"

    def __init__(
        self,
        lister: Lister | None = None,
        show_args: set[str] | None = None,
        *,
        cache: ProcCache | None = None,
        args_mode: str = "allowlist",
        value_flags: frozenset[str] | None = None,
    ) -> None:
        self._args_mode = args_mode
        self._value_flags = VALUE_FLAGS if value_flags is None else frozenset(value_flags)
        self._cache = default_cache(lister, cache)
        self._lister: Lister = lister if lister is not None else default_lister
        self._show_args = set(show_args or ())

    def collect(self, ctx: Ctx) -> SourceResult:
        return collect_procs(
            self._lister, is_gemini, "gemini", self._show_args, self._cache, args_mode=self._args_mode,
            value_flags=self._value_flags,
        )
