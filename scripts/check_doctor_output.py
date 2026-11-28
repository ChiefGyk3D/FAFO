#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Decide whether `fafo doctor` output from a clean install is acceptable.

Usage: check_doctor_output.py EXIT_CODE OUTPUT_FILE   (exit 0 accept, 1 refuse, reasons on stderr)

The rule: doctor must exit 0 or 1 (1 means some source is down or stale, which a clean machine may
have; 2 or higher, a timeout (124) or a signal is a failure), the output must have a `host` line whose
status is `ok` (the host source needs nothing installed, so it must work), and the output must not
contain a Python traceback. Sources such as claude, codex and gemini may be absent. Standard library
only, so it runs before and apart from the package under test.
"""

from __future__ import annotations

import sys
from pathlib import Path

MAX_BYTES = 1_000_000


def check(exit_code: int, text: str) -> list[str]:
    """Return the reasons to refuse; an empty list means accept."""
    problems = []
    if exit_code not in (0, 1):
        problems.append(f"doctor exit code {exit_code} (only 0 or 1 is acceptable)")
    if "Traceback (most recent call last)" in text:
        problems.append("doctor output contains a Python traceback")
    host = [ln.split() for ln in text.splitlines() if ln.split()[:1] == ["host"]]
    if not any(len(p) >= 2 and p[1] == "ok" for p in host):
        problems.append("no `host ok` line in doctor output (the host source must work)")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check_doctor_output.py EXIT_CODE OUTPUT_FILE", file=sys.stderr)
        return 1
    try:
        code = int(argv[1])
        with Path(argv[2]).open("rb") as fh:
            text = fh.read(MAX_BYTES).decode("utf-8", errors="replace")
    except (ValueError, OSError) as exc:
        print(f"check_doctor_output: {type(exc).__name__}", file=sys.stderr)
        return 1
    problems = check(code, text)
    for p in problems:
        print(f"check_doctor_output: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
