"""Perf test options: the default run is CI-friendly, the full variant is opt-in."""

import os
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def pytest_addoption(parser):
    parser.addoption(
        "--perf-full", action="store_true", default=False,
        help="run the full stress variant (30 s window, 8 busy loops)",
    )


@pytest.fixture
def perf_full(request) -> bool:
    return bool(request.config.getoption("--perf-full")) or os.environ.get("FAFO_PERF_FULL") == "1"
