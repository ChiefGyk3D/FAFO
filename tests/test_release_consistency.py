"""Release guards: the tag, the package version and the CHANGELOG line up; release.yml keeps the CI pin."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

import fafo

ROOT = Path(__file__).resolve().parent.parent
# SemVer 2.0.0 (semver.org, the official regex without capture groups): no leading zeroes in the numeric
# parts, prerelease and build metadata allowed. A leading "v" is not part of a version; only the tag has it.
_ID = r"(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
SEMVER = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    rf"(?:-{_ID}(?:\.{_ID})*)?(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?$"
)
VERSION = fafo.__version__
SHARED = re.compile(r"git-your-ship-together/\.github/workflows/\S+@([0-9a-f]{40}) # (v\d+\.\d+\.\d+)\s*$")


def changelog_versions(text: str) -> list[str]:
    return re.findall(r"^## \[([^\]]+)\]", text, flags=re.MULTILINE)


def check_release(version: str, changelog: str, tag: str | None) -> list[str]:
    """Return the problems; an empty list means the release lines up."""
    problems = []
    if not SEMVER.match(version):
        problems.append(f"version {version!r} is not valid SemVer 2.0.0")
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


@pytest.mark.parametrize(
    ("version", "changelog", "tag", "needle"),
    [
        (VERSION, "## [Unreleased]\n", None, f"no section for {VERSION}"),
        (VERSION, f"## [{VERSION}]\n", None, "no Unreleased"),
        (VERSION, f"## [Unreleased]\n## [{VERSION}]\n", "v999.0.0", f"is not v{VERSION}"),
        (VERSION, f"## [Unreleased]\n## [{VERSION}]\n", VERSION, f"is not v{VERSION}"),  # tag needs the v
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


@pytest.mark.parametrize("version", ["0.1.0", "1.0.0", "1.2.3", "1.2.3-rc.1", "1.0.0-0.3.7", "1.2.3+build.5", "10.20.30"])
def test_semver_accepts(version):
    assert SEMVER.match(version)
    assert check_release(version, f"## [Unreleased]\n## [{version}]\n", f"v{version}") == []


@pytest.mark.parametrize("version", ["01.2.3", "1.02.3", "1.2.03", "1.2", "v1.2.3", "1.2.3.4", "1.2.3-01", "1.2.3-", "1.2.3+", ""])
def test_semver_rejects(version):
    assert not SEMVER.match(version)
    assert any("not valid SemVer" in p for p in check_release(version, f"## [Unreleased]\n## [{version}]\n", None))


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


# pypa/gh-action-pypi-publish v1.14.2 is affected by GHSA-5xjx-3w67-w9x9 (a revoked CA in the Sigstore
# chain used for attestations); v1.14.3 is the fix. No workflow may name the vulnerable commit.
VULNERABLE_COMMITS = ("dc37677b2e1c63e2034f94d8a5b11f265b73ba33",)


def vulnerable_hits(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [c for c in VULNERABLE_COMMITS if c in text]


def test_no_workflow_names_a_vulnerable_commit():
    files = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert len(files) >= 2  # something was collected
    assert {f: vulnerable_hits(f) for f in files if vulnerable_hits(f)} == {}


def test_vulnerable_commit_guard_bites(tmp_path):
    bad = tmp_path / "x.yml"
    bad.write_text(
        "      - uses: pypa/gh-action-pypi-publish@" + VULNERABLE_COMMITS[0] + " # v1.14.2\n", encoding="utf-8"
    )
    assert vulnerable_hits(bad) == [VULNERABLE_COMMITS[0]]
    ok = tmp_path / "y.yml"
    ok.write_text("      - uses: pypa/gh-action-pypi-publish@" + "7" * 40 + " # v1.14.3\n", encoding="utf-8")
    assert vulnerable_hits(ok) == []
