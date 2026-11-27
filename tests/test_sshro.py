"""Tests for the read-only ssh helper. Fakes only; the hang test uses a local script, not ssh."""

from __future__ import annotations

import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

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
    assert "Runner.Worker|Runner.Listener" in OPS["workers"]


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
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
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
