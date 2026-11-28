"""Release guards: the tag, the package version and the CHANGELOG line up; release.yml keeps the CI pin."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

import fafo

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
SHARED = re.compile(r"git-your-ship-together/\.github/workflows/\S+@([0-9a-f]{40}) # (v\d+\.\d+\.\d+)\s*$")


def changelog_versions(text: str) -> list[str]:
    return re.findall(r"^## \[([^\]]+)\]", text, flags=re.MULTILINE)


def check_release(version: str, changelog: str, tag: str | None) -> list[str]:
    """Return the problems; an empty list means the release lines up."""
    problems = []
    if not SEMVER.match(version):
        problems.append(f"version {version!r} is not MAJOR.MINOR.PATCH")
    versions = changelog_versions(changelog)
    if "Unreleased" not in versions:
        problems.append("CHANGELOG has no Unreleased section")
    if version not in versions:
        problems.append(f"CHANGELOG has no section for {version}")
    if tag is not None and tag != f"v{version}":
        problems.append(f"tag {tag!r} is not v{version}")
    return problems


def shared_pins(path: Path) -> set[tuple[str, str]]:
    pins = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if "git-your-ship-together/.github/workflows/" in line and "uses:" in line:
            m = SHARED.search(line)
            assert m, f"{path.name}: shared workflow call without a 40-hex pin and a version comment: {line}"
            pins.add((m.group(1), m.group(2)))
    return pins


def test_version_changelog_and_tag_line_up():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    tag = os.environ.get("GITHUB_REF_NAME") if os.environ.get("GITHUB_REF_TYPE") == "tag" else None
    assert check_release(fafo.__version__, changelog, tag) == []


def test_first_release_is_0_1_0():
    assert fafo.__version__ == "0.1.0"


@pytest.mark.parametrize(
    ("version", "changelog", "tag", "needle"),
    [
        ("0.1.0", "## [Unreleased]\n", None, "no section for 0.1.0"),
        ("0.1.0", "## [0.1.0]\n", None, "no Unreleased"),
        ("0.1.0", "## [Unreleased]\n## [0.1.0]\n", "v0.2.0", "is not v0.1.0"),
        ("0.1", "## [Unreleased]\n## [0.1]\n", None, "not MAJOR.MINOR.PATCH"),
    ],
)
def test_guard_bites(version, changelog, tag, needle):
    problems = check_release(version, changelog, tag)
    assert any(needle in p for p in problems), problems


def test_release_workflow_uses_the_ci_pin():
    wf = ROOT / ".github" / "workflows"
    files = sorted(wf.glob("*.yml"))
    assert len(files) >= 2  # something was collected
    pins = set()
    for f in files:
        pins |= shared_pins(f)
    assert len(pins) == 1, f"shared workflow callers must share one pinned commit: {pins}"
    assert shared_pins(wf / "release.yml"), "release.yml calls no shared workflow"


def test_pin_guard_bites(tmp_path):
    bad = tmp_path / "x.yml"
    bad.write_text("    uses: ChiefGyk3D/git-your-ship-together/.github/workflows/a.yml@main\n", encoding="utf-8")
    with pytest.raises(AssertionError):
        shared_pins(bad)
    nover = tmp_path / "y.yml"
    nover.write_text(
        "    uses: ChiefGyk3D/git-your-ship-together/.github/workflows/a.yml@" + "a" * 40 + "\n", encoding="utf-8"
    )
    with pytest.raises(AssertionError):
        shared_pins(nover)


def test_pypi_job_is_gated_and_direct():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "vars.PYPI_PUBLISH == 'true'" in text
    assert "startsWith(github.ref, 'refs/tags/v')" in text
    assert re.search(r"uses: pypa/gh-action-pypi-publish@[0-9a-f]{40} # v\d+\.\d+\.\d+", text)
    assert "environment:" in text and "name: pypi" in text
