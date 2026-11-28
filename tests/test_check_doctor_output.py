"""scripts/check_doctor_output.py decides whether `fafo doctor` output from a clean install is acceptable."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_doctor_output.py"


def load():
    spec = importlib.util.spec_from_file_location("check_doctor_output", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OK = "host ok no rows\nclaude absent no rows\ncodex ok procs=6\n"


def test_ok_output_passes():
    assert load().check(0, OK) == []


def test_exit_1_with_host_ok_passes():
    # another source being down is allowed on a clean machine; the host source must work
    assert load().check(1, "host ok x\ncodex down ConnectionError\n") == []


@pytest.mark.parametrize(
    ("code", "text", "needle"),
    [
        (0, "host down OSError\n", "host"),
        (0, "claude absent x\n", "host"),
        (0, "", "host"),
        (0, "host absent psutil missing\n", "host"),
        (0, "hostile ok x\n", "host"),
        (0, OK + "Traceback (most recent call last):\n  File x\n", "traceback"),
        (1, "Traceback (most recent call last):\nhost ok x\n", "traceback"),
        (2, OK, "exit code 2"),
        (3, OK, "exit code 3"),
        (124, OK, "exit code 124"),
        (127, OK, "exit code 127"),
        (-9, OK, "exit code -9"),
    ],
)
def test_bad_results_fail(code, text, needle):
    problems = load().check(code, text)
    assert problems and any(needle in p for p in problems), problems


def run_cli(tmp_path, code, text):
    f = tmp_path / "out.txt"
    f.write_text(text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(code), str(f)],
        capture_output=True, text=True, check=False, timeout=30, stdin=subprocess.DEVNULL,
    )


def test_cli_exit_codes(tmp_path):
    assert run_cli(tmp_path, 0, OK).returncode == 0
    bad = run_cli(tmp_path, 2, OK)
    assert bad.returncode == 1 and "exit code 2" in bad.stderr
    assert run_cli(tmp_path, "x", OK).returncode == 1  # not a number: fail closed
    missing = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "0", str(tmp_path / "nope")],
        capture_output=True, text=True, check=False, timeout=30, stdin=subprocess.DEVNULL,
    )
    assert missing.returncode == 1
