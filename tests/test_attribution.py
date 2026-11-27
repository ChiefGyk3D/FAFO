"""Project attribution tests. Real git runs only in temporary repos."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import pytest
from conftest import Ran, deadline_bound, run_when_ready, wait_gone

from fafo import attribution
from fafo.attribution import SubprocessGit, attribute

TOKEN = "ghp_SECRETTOKEN0123456789"


@pytest.fixture(autouse=True)
def _fresh_cache():
    attribution.clear_cache()
    yield
    attribution.clear_cache()


def _git(cwd: Path, *args: str) -> None:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(cwd),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env, capture_output=True)


def make_repo(path: Path, remote: str | None = None) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    if remote is not None:
        _git(path, "remote", "add", "origin", remote)
    return path


class FakeGit:
    """Scripted runner: maps (first arg, cwd) to a result and counts calls."""

    def __init__(self, table: dict[tuple[str, str], str | None] | None = None):
        self.table = table or {}
        self.calls: list[tuple[list[str], str]] = []

    def run(self, args: list[str], cwd: str, timeout: float) -> str | None:
        self.calls.append((args, cwd))
        return self.table.get((args[0], cwd))


def test_three_sessions_same_cwd_three_repos(tmp_path: Path):
    parent = tmp_path / "src"
    parent.mkdir()
    repos = [make_repo(parent / n) for n in ("alpha", "beta", "gamma")]
    got = [attribute(str(parent), [str(r / "sub")] + [str(r)]) for r in repos]
    assert [g[0] for g in got] == ["alpha", "beta", "gamma"]
    assert {g[1] for g in got} == {"children-git"}


def test_most_common_child_repo_wins(tmp_path: Path):
    a = make_repo(tmp_path / "a")
    b = make_repo(tmp_path / "b")
    assert attribute(None, [str(a), str(b), str(b)]) == ("b", "children-git")


def test_deleted_cwd_falls_through_without_raising(tmp_path: Path):
    gone = tmp_path / "gone"
    git = FakeGit()
    assert attribute(str(gone), [str(gone / "x")], git=git) == ("gone", "cwd-name")
    assert git.calls == []


def test_relative_path_skipped_without_git(tmp_path: Path, monkeypatch):
    make_repo(tmp_path / "r")
    monkeypatch.chdir(tmp_path)
    git = FakeGit()
    assert attribute("r", ["r"], git=git) == ("r", "cwd-name")
    assert git.calls == []


def test_file_cwd_skipped_without_git(tmp_path: Path):
    f = tmp_path / "afile"
    f.write_text("x")
    git = FakeGit()
    attribute(str(f), [str(f)], git=git)
    assert git.calls == []


def test_adapter_wins_over_git(tmp_path: Path):
    r = make_repo(tmp_path / "repo")
    git = FakeGit()
    assert attribute(str(r), [str(r)], "board-project", git=git) == ("board-project", "adapter")
    assert git.calls == []


def test_empty_adapter_does_not_win(tmp_path: Path):
    r = make_repo(tmp_path / "repo")
    assert attribute(str(r), [], "") == ("repo", "cwd-git")


def test_cwd_git_when_no_children(tmp_path: Path):
    r = make_repo(tmp_path / "repo")
    (r / "deep").mkdir()
    assert attribute(str(r / "deep"), []) == ("repo", "cwd-git")


def test_cwd_name_outside_any_repo(tmp_path: Path):
    d = tmp_path / "plain"
    d.mkdir()
    git = FakeGit()  # not a repo: rev-parse yields None
    assert attribute(str(d), [], git=git) == ("plain", "cwd-name")


def test_unknown_when_no_cwd():
    assert attribute(None, []) == ("unknown", "cwd-name")


def test_remote_name_used_over_toplevel(tmp_path: Path):
    r = make_repo(tmp_path / "checkout-dir", "https://example.com/org/realname.git")
    assert attribute(str(r), []) == ("realname", "cwd-git")


def test_credentialed_remote_yields_repo_only(tmp_path: Path, caplog):
    url = f"https://user:{TOKEN}@host.example/org/repo.git"
    r = make_repo(tmp_path / "w", url)
    with caplog.at_level(logging.DEBUG):
        result = attribute(str(r), [str(r)])
    assert result == ("repo", "children-git")
    assert TOKEN not in repr(result) and "user" not in result[0]
    assert TOKEN not in caplog.text


def test_credentialed_remote_with_query_and_odd_name():
    url = f"https://u:{TOKEN}@h/org/repo.git?x={TOKEN}"
    assert attribution._name_from_remote(url) == "repo"
    assert attribution._name_from_remote(f"https://h/{TOKEN} space") is None


def test_ssh_style_remote(tmp_path: Path):
    r = make_repo(tmp_path / "w", "git@host.example:org/repo.git")
    assert attribute(str(r), []) == ("repo", "cwd-git")


def test_ssh_url_and_trailing_slash():
    assert attribution._name_from_remote("ssh://git@h:22/org/repo.git/") == "repo"
    assert attribution._name_from_remote("user:pw@h:org/repo") == "repo"


def test_unparseable_remote_falls_back_to_toplevel_basename(tmp_path: Path):
    r = make_repo(tmp_path / "topname", "https://h/org/bad name!.git")
    result = attribute(str(r), [])
    assert result == ("topname", "cwd-git")


def test_git_timeout_returns_fallback_quickly(tmp_path: Path):
    d = tmp_path / "slow"
    d.mkdir()
    fake_git = tmp_path / "fakegit"
    fake_git.write_text("#!/bin/sh\nsleep 30\n")
    fake_git.chmod(0o700)
    runner = SubprocessGit(git_bin=str(fake_git))
    start = time.monotonic()
    assert runner.run(["rev-parse", "--show-toplevel"], str(d), 1.0) is None
    assert 1.0 <= time.monotonic() - start < deadline_bound(1.0)
    assert attribute(str(d), [], git=runner) == ("slow", "cwd-name")


def test_runner_returns_none_when_binary_missing(tmp_path: Path):
    d = tmp_path / "x"
    d.mkdir()
    assert SubprocessGit(git_bin=str(tmp_path / "nope")).run(["x"], str(d), 1) is None


def test_caching_with_injected_clock(tmp_path: Path):
    r = make_repo(tmp_path / "repo")
    d = str(r)
    git = FakeGit({("rev-parse", d): d, ("config", d): None})
    clock = [100.0]

    def now() -> float:
        return clock[0]

    attribute(d, [], git=git, now=now)
    n = len(git.calls)
    assert n == 2
    clock[0] = 129.0
    attribute(d, [], git=git, now=now)
    assert len(git.calls) == n
    clock[0] = 131.0
    attribute(d, [], git=git, now=now)
    assert len(git.calls) == 2 * n


def test_control_characters_in_dir_name_are_stripped(tmp_path: Path):
    name = "ev" + chr(27) + "]0;pwn" + chr(7) + "il" + chr(10) + "x"
    d = tmp_path / name
    d.mkdir()
    project, rule = attribute(str(d), [])
    assert rule == "cwd-name"
    assert all(ord(c) >= 32 and not 127 <= ord(c) <= 159 for c in project)
    # also via a git toplevel with such a name
    r = make_repo(tmp_path / ("r" + chr(27) + "[31mx"))
    project, rule = attribute(str(r), [])
    assert rule == "cwd-git"
    assert chr(27) not in project


def test_control_characters_in_remote_fall_back(tmp_path: Path):
    assert attribution._name_from_remote("https://h/org/re" + chr(27) + "po.git") is None
    assert attribution._name_from_remote("https://h/org/re" + chr(10) + "po.git") is None


def test_adapter_name_with_control_chars_is_rejected():
    assert attribute(None, [], "pr" + chr(27) + "oj") == ("unknown", "cwd-name")


def test_oversized_output_is_handled(tmp_path: Path):
    d = tmp_path / "big"
    d.mkdir()
    fake_git = tmp_path / "biggit"
    fake_git.write_text("#!/bin/sh\nhead -c 5000 /dev/zero | tr '\\0' a\n")
    fake_git.chmod(0o700)
    runner = SubprocessGit(git_bin=str(fake_git))
    assert runner.run(["rev-parse"], str(d), 2) is None
    assert attribute(str(d), [], git=runner) == ("big", "cwd-name")


def test_oversized_fake_output_is_not_trusted(tmp_path: Path):
    d = tmp_path / "w"
    d.mkdir()
    git = FakeGit({("rev-parse", str(d)): "/" + "a" * 5000, ("config", str(d)): "b" * 5000})
    project, _rule = attribute(str(d), [], git=git)
    assert len(project) <= 100
    assert "b" * 200 not in project


def test_adapter_credentialed_url_reduced_to_name(caplog):
    url = f"https://user:{TOKEN}@host.example/org/repo.git"
    result = attribute(None, [], url)
    assert result == ("repo", "adapter")
    assert TOKEN not in repr(result)


def test_adapter_scp_style_reduced_to_name():
    assert attribute(None, [], "git@host:org/repo.git") == ("repo", "adapter")


def test_adapter_with_spaces_or_controls_rejected_and_falls_through(tmp_path: Path):
    r = make_repo(tmp_path / "gitname")
    for bad in ("my project", "pr" + chr(27) + "oj", "a" + chr(10) + "b", "x" * 101):
        assert attribute(str(r), [], bad) == ("gitname", "cwd-git")
    assert attribute(None, [], "my project") == ("unknown", "cwd-name")


def test_adapter_plain_name_accepted():
    assert attribute(None, [], "my-project") == ("my-project", "adapter")


def test_timeout_kills_whole_process_group(tmp_path: Path):
    d = tmp_path / "w"
    d.mkdir()

    def scenario(deadline):
        pidfile = tmp_path / f"child-{deadline}.pid"
        script = tmp_path / f"grouplaunch-{deadline}"
        script.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {pidfile}\nsleep 30\n")
        script.chmod(0o700)
        t0 = time.monotonic()
        res = SubprocessGit(git_bin=str(script)).run(["x"], str(d), deadline)
        return Ran((res, time.monotonic() - t0), pidfile)

    r = run_when_ready(scenario)
    assert r.result[0] is None
    assert r.result[1] < deadline_bound(r.deadline)
    assert wait_gone(r.pids), "grandchild survived the group kill"


def test_oversize_kills_whole_process_group(tmp_path: Path):
    d = tmp_path / "w"
    d.mkdir()

    def scenario(deadline):
        pidfile = tmp_path / f"child-{deadline}.pid"
        script = tmp_path / f"bigblocker-{deadline}"
        script.write_text(
            f"#!/bin/sh\nsleep 30 &\necho $! > {pidfile}\n"
            "head -c 5000 /dev/zero | tr '\\0' a\nsleep 30\n"
        )
        script.chmod(0o700)
        res = SubprocessGit(git_bin=str(script)).run(["x"], str(d), max(deadline, 10.0))
        return Ran(res, pidfile)

    r = run_when_ready(scenario)
    assert r.result is None
    assert wait_gone(r.pids), "grandchild survived the group kill"


def test_global_git_config_cannot_supply_remote(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[remote \"origin\"]\n\turl = https://h/org/decoy.git\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    r = make_repo(tmp_path / "toplevelname")
    assert attribute(str(r), []) == ("toplevelname", "cwd-git")


def test_normal_exit_kills_group_once_before_reap(tmp_path: Path, group_kill_spy):
    d = tmp_path / "w"
    d.mkdir()
    script = tmp_path / "fastgit"
    script.write_text("#!/bin/sh\necho hello\n")
    script.chmod(0o700)
    assert SubprocessGit(git_bin=str(script)).run(["x"], str(d), 10) == "hello"
    kills = group_kill_spy.kills()
    assert len(kills) == 1
    assert group_kill_spy.events[0] == kills[0]  # no wait before the kill
    assert group_kill_spy.reaped_when_killed == []


def test_normal_exit_leaves_no_grandchild(tmp_path: Path):
    d = tmp_path / "w"
    d.mkdir()

    def scenario(deadline):
        pidfile = tmp_path / f"child-{deadline}.pid"
        script = tmp_path / f"leaver-{deadline}"
        script.write_text(
            f"#!/bin/sh\nsleep 30 >/dev/null 2>&1 &\necho $! > {pidfile}\necho hello\n"
        )
        script.chmod(0o700)
        res = SubprocessGit(git_bin=str(script)).run(["x"], str(d), max(deadline, 10.0))
        return Ran(res, pidfile)

    r = run_when_ready(scenario)
    assert r.result == "hello"
    assert wait_gone(r.pids), "grandchild survived the group kill"
