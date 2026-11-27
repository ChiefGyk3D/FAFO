"""Repo-wide guard: no em dash (U+2014) in any tracked text file."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EM = chr(0x2014)
TOP = ("src", "tests", "docs", "README.md", "config.example.toml")


def tracked_text_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", *TOP],
        capture_output=True, check=True, timeout=30, stdin=subprocess.DEVNULL,
    ).stdout.decode()
    return [ROOT / n for n in out.split("\0") if n]


def find_em_dashes(files: list[Path]) -> list[str]:
    hits = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: not a text file
        for n, line in enumerate(text.splitlines(), 1):
            if EM in line:
                hits.append(f"{f.relative_to(ROOT) if f.is_relative_to(ROOT) else f}:{n}")
    return hits


def test_no_em_dash_in_tracked_text_files():
    files = tracked_text_files()
    assert len(files) > 20  # something was collected
    assert find_em_dashes(files) == []


def test_guard_bites(tmp_path):
    bad = tmp_path / "x.txt"
    bad.write_text("fine\nbad " + EM + " here\n", encoding="utf-8")
    ok = tmp_path / "y.txt"
    ok.write_text("plain - ascii\n", encoding="utf-8")
    hits = find_em_dashes([ok, bad])
    assert len(hits) == 1 and hits[0].endswith("x.txt:2")
