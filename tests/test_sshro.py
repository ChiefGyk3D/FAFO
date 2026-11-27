"""Tests for the read-only ssh helper. Fakes only; the hang test uses a local script, not ssh."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest
from conftest import wait_gone

from fafo.sshro import OPS, SshError, SshHost, build_argv, default_runner, ssh_ro

HOST = SshHost(alias="runner-a", key_path="/keys/id_runner", user="ci")
ESC = chr(27)


class Fake:
    def __init__(self, rc: int = 0, out: bytes = b"ok\n", exc: Exception | None = None) -> None:
        self.rc, self.out, self.exc = rc, out, exc
        self.calls: list[tuple] = []

    def __call__(self, argv, timeout_s, env):
        self.calls.append((list(argv), timeout_s, dict(env)))
        if self.exc:
            raise self.exc
        return self.rc, self.out


def test_unknown_op_rejected_and_runner_not_called():
    fake = Fake()
    for bad in ("rm -rf /", "loadavg; id", "", "LOADAVG", "df -h", "workers\n"):
        with pytest.raises(ValueError):
            ssh_ro(HOST, bad, runner=fake)
    assert fake.calls == []


@pytest.mark.parametrize(
    "alias",
    ["-oProxyCommand=x", "a b", "a;b", "", "a" * 101, "a\nb", "$(id)", "a`b`", "ho/st"],
)
def test_bad_alias_rejected(alias):
    fake = Fake()
    with pytest.raises(ValueError):
        ssh_ro(SshHost(alias=alias), "loadavg", runner=fake)
    assert fake.calls == []


def test_bad_user_rejected():
    with pytest.raises(ValueError):
        ssh_ro(SshHost(alias="runner-a", user="-oFoo=1"), "loadavg", runner=Fake())


def test_argv_is_list_with_hardening_options_and_fixed_command():
    fake = Fake()
    assert ssh_ro(HOST, "loadavg", runner=fake) == "ok\n"
    argv = fake.calls[0][0]
    assert isinstance(argv, list) and argv[0] == "ssh"
    for opt in (
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "IdentitiesOnly=yes",
        "ClearAllForwardings=yes",
        "ControlMaster=no",
        "RequestTTY=no",
        "ConnectTimeout=4",
    ):
        assert opt in argv, opt
    assert argv[argv.index("-i") + 1] == "/keys/id_runner"
    assert "ci@runner-a" in argv
    assert argv[-1] == OPS["loadavg"] == "cat /proc/loadavg"
    assert not any(a in ("sh", "bash", "-c") for a in argv)
    assert "--" not in argv


def test_every_op_is_one_fixed_string():
    assert set(OPS) == {"loadavg", "meminfo", "df", "workers"}
    for op, cmd in OPS.items():
        argv = build_argv(SshHost(alias="runner-a"), op)
        assert argv[-1] == cmd
    # bracket trick: the pattern must not match the grep process's own command line
    assert "'[R]unner.Worker|[R]unner.Listener'" in OPS["workers"]
    assert "Runner.Worker|Runner.Listener" not in OPS["workers"]


def test_env_is_minimal(monkeypatch):
    monkeypatch.setenv("SOME_OTHER_SENTINEL", "x")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")
    fake = Fake()
    ssh_ro(HOST, "df", runner=fake)
    env = fake.calls[0][2]
    assert set(env) <= {"PATH", "HOME", "LANG", "SSH_AUTH_SOCK"}
    assert env["LANG"] == "C"
    assert env["SSH_AUTH_SOCK"] == "/run/agent.sock"
    monkeypatch.delenv("SSH_AUTH_SOCK")
    ssh_ro(HOST, "df", runner=fake)
    assert "SSH_AUTH_SOCK" not in fake.calls[1][2]


def test_hostkey_change_raises_ssherror_never_prompts():
    text = ("WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!" + ESC + "[31m token=abc\n").encode()
    fake = Fake(rc=255, out=text)
    with pytest.raises(SshError) as ei:
        ssh_ro(HOST, "loadavg", runner=fake)
    msg = str(ei.value)
    assert len(msg) <= 200 and ESC not in msg and "abc" not in msg
    argv = fake.calls[0][0]
    assert "BatchMode=yes" in argv and "StrictHostKeyChecking=yes" in argv


def test_nonzero_exit_and_runner_errors_become_ssherror():
    with pytest.raises(SshError):
        ssh_ro(HOST, "df", runner=Fake(rc=1))
    with pytest.raises(SshError):
        ssh_ro(HOST, "df", runner=Fake(exc=subprocess.TimeoutExpired(["ssh"], 1)))
    with pytest.raises(SshError):
        ssh_ro(HOST, "df", runner=Fake(exc=FileNotFoundError("ssh")))
    with pytest.raises(SshError):
        ssh_ro(HOST, "df", runner=Fake(out=b"x" * (64 * 1024 + 1)))


def _script(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "fake-ssh.sh"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(stat.S_IRWXU)
    return p


def test_hung_child_cut_by_deadline_and_group_killed(tmp_path):
    pidfile = tmp_path / "child.pid"
    script = _script(tmp_path, f"sleep 30 &\necho $! > {pidfile}\nwait\n")

    def runner(argv, timeout_s, env):
        return default_runner([str(script)], timeout_s, env)

    result: dict = {}

    def target():
        t0 = time.monotonic()
        try:
            ssh_ro(HOST, "loadavg", timeout_s=0.7, runner=runner)
        except SshError as exc:
            result["err"] = exc
        result["elapsed"] = time.monotonic() - t0

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(8)
    assert not th.is_alive(), "ssh deadline did not fire"
    assert "err" in result and result["elapsed"] < 5
    pid = int(pidfile.read_text())
    if not wait_gone([pid]):
        os.kill(pid, 9)
        pytest.fail("child process survived the group kill")


def test_default_runner_returns_exit_code_and_output(tmp_path):
    script = _script(tmp_path, "echo hello\nexit 3\n")
    rc, out = default_runner([str(script)], 5.0, {"PATH": os.environ["PATH"]})
    assert (rc, out) == (3, b"hello\n")


def test_workers_no_match_is_empty_not_error():
    assert ssh_ro(HOST, "workers", runner=Fake(rc=1, out=b"")) == ""
    with pytest.raises(SshError):
        ssh_ro(HOST, "df", runner=Fake(rc=1, out=b""))


def test_grep_pattern_cannot_match_its_own_command_line():
    import re

    cmd = OPS["workers"]
    pattern = cmd.split("grep -E ")[1].strip("'")
    self_line = f"grep -E '{pattern}'"
    assert re.search(pattern, self_line) is None


ENV = {"PATH": os.environ["PATH"]}


def test_normal_exit_kills_group_once_before_reap(tmp_path, group_kill_spy):
    script = _script(tmp_path, "echo hi\n")
    assert default_runner([str(script)], 5.0, ENV) == (0, b"hi\n")
    kills = group_kill_spy.kills()
    assert len(kills) == 1
    assert group_kill_spy.reaped_when_killed == []
    pid = kills[0][1]
    kill_at = group_kill_spy.events.index(kills[0])
    assert ("wait", pid) in group_kill_spy.events[kill_at:]


def test_normal_exit_kills_grandchild_left_behind(tmp_path):
    pidfile = tmp_path / "gc.pid"
    script = _script(tmp_path, f"sleep 30 >/dev/null 2>&1 &\necho $! > {pidfile}\necho done\n")
    assert default_runner([str(script)], 5.0, ENV) == (0, b"done\n")
    pid = int(pidfile.read_text())
    if not wait_gone([pid]):
        os.kill(pid, signal.SIGKILL)
        pytest.fail("grandchild outlived a normally exited child")


def test_hung_child_and_grandchild_killed_within_deadline(tmp_path, group_kill_spy):
    pidfile = tmp_path / "gc.pid"
    script = _script(tmp_path, f"sleep 30 &\necho $! > {pidfile}\nwait\n")
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        default_runner([str(script)], 0.5, ENV)
    assert time.monotonic() - t0 < 5
    assert len(group_kill_spy.kills()) >= 1
    assert group_kill_spy.reaped_when_killed == []
    pid = int(pidfile.read_text())
    if not wait_gone([pid]):
        os.kill(pid, signal.SIGKILL)
        pytest.fail("grandchild survived the deadline kill")


def test_externally_reaped_child_gets_no_signal(tmp_path, group_kill_spy, monkeypatch):
    real_wait = os.waitid
    script = _script(tmp_path, "echo hi\n")
    real_popen = subprocess.Popen
    holder: list = []

    class P(real_popen):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            holder.append(self)

    monkeypatch.setattr("fafo.sshro.subprocess.Popen", P)

    # Reap the child behind Popen's back as soon as it has exited, before the runner looks.
    def waitid(idtype, pid, flags):
        if holder and pid == holder[0].pid and flags & os.WNOWAIT:
            try:
                real_wait(os.P_PID, pid, os.WEXITED)  # blocks until exit
                os.waitpid(pid, 0)  # external reap
            except ChildProcessError:
                pass
        return real_wait(idtype, pid, flags)

    monkeypatch.setattr("fafo.procutil.os.waitid", waitid)
    default_runner([str(script)], 5.0, ENV)
    assert group_kill_spy.kills() == []
