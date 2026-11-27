"""CLI: doctor output and exit codes, run start/stop under real signals, no leaks."""

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

import fafo
from fafo import cli
from fafo.ghapi import GhClient
from fafo.source import Ctx, SourceResult
from fafo.sources.runners import RunnersCfg, RunnersSource
from fafo.sshro import SshError

SENTINEL = "SENTINEL-TOKEN-VALUE-0123456789"
NAMES = ("host", "claude", "codex", "gemini", "ollama", "runners", "copilot")
OWNER_RW = stat.S_IRUSR | stat.S_IWUSR


def conf(tmp_path: Path, *enabled: str, extra: str = "", top: str = "") -> Path:
    text = top
    for n in NAMES:
        text += f"[sources.{n}]\nenabled = {'true' if n in enabled else 'false'}\n"
        text += "interval_s = 0.2\ntimeout_s = 5\n"
        if n == "ollama" and n in enabled:
            text += 'url = "http://ollama.example.invalid:11434"\n'
    p = tmp_path / "fafo.toml"
    p.write_text(text + extra, encoding="utf-8")
    return p


RELEASE = threading.Event()  # lets abandoned doctor workers end so no thread leaks across tests


@pytest.fixture(autouse=True)
def _release_hung_workers():
    RELEASE.clear()
    yield
    RELEASE.set()
    end = time.monotonic() + 3
    while time.monotonic() < end:
        if not [t for t in threading.enumerate() if t.name.startswith("fafo-")]:
            return
        time.sleep(0.02)
    raise AssertionError("leaked fafo threads")


class Stub:
    def __init__(self, name, result=None, exc=None, hang=False):
        self.name, self._result, self._exc, self._hang = name, result, exc, hang

    def collect(self, ctx: Ctx) -> SourceResult:
        if self._hang:
            RELEASE.wait(3600)
        if self._exc is not None:
            raise self._exc
        return self._result if self._result is not None else SourceResult()


def run_main(argv, factories, capsys):
    code = cli.main(argv, factories=factories)
    out = capsys.readouterr()
    return code, out.out, out.err


# --- basics ---------------------------------------------------------------------------------


def test_version(capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["--version"])
    assert ei.value.code == 0
    assert fafo.__version__ in capsys.readouterr().out


def test_extension_hook_is_a_noop():
    import argparse

    sub = argparse.ArgumentParser().add_subparsers()
    before = dict(sub.choices or {})
    cli.add_render_commands(sub)
    assert dict(sub.choices or {}) == before


def test_no_subcommand_is_usage_error():
    with pytest.raises(SystemExit) as ei:
        cli.main([])
    assert ei.value.code == 2


@pytest.mark.parametrize("cmd", ["run", "doctor"])
def test_missing_config_file_exit_2_clean_message(tmp_path, capsys, cmd):
    missing = tmp_path / "nope.toml"
    code, out, err = run_main([cmd, "--config", str(missing)], None, capsys)
    assert code == 2
    assert "Traceback" not in err and "Traceback" not in out
    assert "config" in err and "not found" in err


def test_bad_option_exit_2_names_source_no_value(tmp_path, capsys):
    p = conf(tmp_path, "ollama")
    p.write_text(p.read_text().replace("http://ollama.example.invalid:11434", "ftp://hunter2.invalid"))
    code, out, err = run_main(["doctor", "--config", str(p)], None, capsys)
    assert code == 2
    assert "sources.ollama" in err
    assert "hunter2" not in err + out


def test_unexpected_error_exit_1_without_values(tmp_path, capsys):
    def boom(s, c, e):
        raise RuntimeError("explode hunter2")

    p = conf(tmp_path, "codex")
    code, out, err = run_main(["doctor", "--config", str(p)], {"codex": boom}, capsys)
    assert code == 1
    assert "hunter2" not in err + out
    assert "RuntimeError" in err


# --- doctor ---------------------------------------------------------------------------------


def test_doctor_all_ok_or_absent_exit_0_and_line_shape(tmp_path, capsys):
    f = {
        "codex": lambda s, c, e: Stub("codex", SourceResult(status="ok")),
        "gemini": lambda s, c, e: Stub("gemini", SourceResult(status="absent")),
    }
    code, out, _ = run_main(["doctor", "--config", str(conf(tmp_path, "codex", "gemini"))], f, capsys)
    lines = out.splitlines()
    assert code == 0
    assert [ln.split()[0] for ln in lines] == ["codex", "gemini"]
    assert lines[0].split()[1] == "ok" and lines[1].split()[1] == "absent"
    assert all(len(ln.split(None, 2)) >= 2 for ln in lines)


def test_doctor_down_exit_1(tmp_path, capsys):
    f = {
        "codex": lambda s, c, e: Stub("codex", exc=ConnectionError("refused hunter2")),
        "gemini": lambda s, c, e: Stub("gemini"),
    }
    code, out, _ = run_main(["doctor", "--config", str(conf(tmp_path, "codex", "gemini"))], f, capsys)
    assert code == 1
    by = {ln.split()[0]: ln.split()[1] for ln in out.splitlines()}
    assert by == {"codex": "down", "gemini": "ok"}
    assert "hunter2" not in out
    assert "ConnectionError" in out


def test_doctor_hung_source_is_stale_and_does_not_block(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "DOCTOR_TIMEOUT_S", 0.3)
    f = {
        "codex": lambda s, c, e: Stub("codex", hang=True),
        "gemini": lambda s, c, e: Stub("gemini", hang=True),
    }
    t0 = time.monotonic()
    code, out, _ = run_main(["doctor", "--config", str(conf(tmp_path, "codex", "gemini"))], f, capsys)
    assert time.monotonic() - t0 < 2.0  # parallel, abandoned on timeout
    assert code == 1
    assert [ln.split()[1] for ln in out.splitlines()] == ["stale", "stale"]


def test_doctor_skipped_source_prints_absent_with_reason(tmp_path, capsys):
    def nodep(s, c, e):
        raise ImportError("x", name="psutil")

    code, out, _ = run_main(["doctor", "--config", str(conf(tmp_path, "host"))], {"host": nodep}, capsys)
    assert code == 0
    assert out.split()[:2] == ["host", "absent"]
    assert "psutil" in out


def test_doctor_hostnames_shown_through_alias_and_details_capped(tmp_path, capsys):
    host = "runner-9.example.invalid"
    top = f'[aliases]\n"{host}" = "runner-9"\n'
    long = "x" * 5000
    f = {
        "codex": lambda s, c, e: Stub("codex", exc=SshError(f"connect to {host} failed {long}")),
    }
    code, out, _ = run_main(["doctor", "--config", str(conf(tmp_path, "codex", top=top))], f, capsys)
    assert code == 1
    assert host not in out
    assert "runner-9" in out
    assert len(out.splitlines()) == 1 and len(out) < 400


def test_doctor_escape_sequences_stripped(tmp_path, capsys):
    esc = chr(27)
    f = {"codex": lambda s, c, e: Stub("codex", exc=SshError(f"a{esc}]0;pwn{chr(7)}b{chr(10)}c"))}
    _, out, _ = run_main(["doctor", "--config", str(conf(tmp_path, "codex"))], f, capsys)
    assert esc not in out and "pwn" not in out
    assert len(out.splitlines()) == 1


class FakeHttp:
    """Stands in for the network; records what the real client sent."""

    def __init__(self):
        self.sent: list[dict] = []

    def __call__(self, method, url, headers, timeout):
        self.sent.append(dict(headers))
        return 200, {}, json.dumps({"workflow_runs": []}).encode()


def test_token_name_read_value_never_printed_by_doctor(tmp_path, capsys, monkeypatch, caplog):
    monkeypatch.setenv("FAFO_TEST_GH", SENTINEL)
    http = FakeHttp()

    def runners(s, c, e):
        gh = GhClient("FAFO_TEST_GH", http=http)
        return RunnersSource(RunnersCfg(repos=["example-org/example-repo"], token_env="FAFO_TEST_GH"), gh)

    caplog.set_level("DEBUG")
    code, out, err = run_main(
        ["doctor", "--config", str(conf(tmp_path, "runners"))], {"runners": runners}, capsys
    )
    assert code == 0
    assert http.sent and SENTINEL in http.sent[0]["Authorization"]  # the value really was used
    assert SENTINEL not in out + err + caplog.text


def test_token_value_not_in_runtime_failure_output(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("FAFO_TEST_GH", SENTINEL)

    def failing_http(method, url, headers, timeout):
        raise OSError(f"connect failed {headers['Authorization']}")

    def runners(s, c, e):
        gh = GhClient("FAFO_TEST_GH", http=failing_http)
        return RunnersSource(RunnersCfg(repos=["example-org/example-repo"], token_env="FAFO_TEST_GH"), gh)

    code, out, err = run_main(
        ["doctor", "--config", str(conf(tmp_path, "runners"))], {"runners": runners}, capsys
    )
    assert code == 1
    assert SENTINEL not in out + err


def test_doctor_runners_ssh_note_makes_it_down_not_ok(tmp_path, capsys):
    class Noted(Stub):
        note = "runner-1: ssh connection failed"

    f = {"runners": lambda s, c, e: Noted("runners")}
    code, out, _ = run_main(["doctor", "--config", str(conf(tmp_path, "runners"))], f, capsys)
    assert code == 1
    assert out.split()[:2] == ["runners", "down"]


# --- run: real signals, real snapshot -------------------------------------------------------

HANG_SCRIPT = textwrap.dedent(
    """
    import sys, threading
    from fafo import cli

    class Hang:
        name = "codex"
        def collect(self, ctx):
            threading.Event().wait(10 ** 6)

    sys.exit(cli.main(sys.argv[1:], factories={"codex": lambda s, c, e: Hang()}))
    """
)


def wait_for(path: Path, proc: subprocess.Popen, limit: float = 15.0) -> None:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if path.exists():
            return
        if proc.poll() is not None:
            raise AssertionError(f"process exited early: {proc.returncode}")
        time.sleep(0.05)
    raise AssertionError("snapshot never appeared")


def stop_and_time(proc: subprocess.Popen, sig: int) -> tuple[int, float]:
    t0 = time.monotonic()
    proc.send_signal(sig)
    try:
        rc = proc.wait(timeout=6)  # guard: a regression fails here instead of hanging the suite
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise AssertionError("process did not stop after the signal") from None
    return rc, time.monotonic() - t0


@pytest.fixture
def child_env():
    env = dict(os.environ)
    env["FAFO_TEST_GH"] = SENTINEL
    return env


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_run_stops_within_2s_with_forever_hung_source(tmp_path, child_env, sig):
    script = tmp_path / "hang_main.py"
    script.write_text(HANG_SCRIPT, encoding="utf-8")
    snap = tmp_path / "state" / "snapshot.json"
    cfgp = conf(tmp_path, "codex")
    proc = subprocess.Popen(
        [sys.executable, str(script), "run", "--config", str(cfgp), "--snapshot", str(snap)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env,
    )
    try:
        wait_for(snap, proc)
        time.sleep(0.5)  # let the hung call start
        rc, took = stop_and_time(proc, sig)
        out, err = proc.communicate()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert rc == 0
    assert took < 2.0
    assert stat.S_IMODE(snap.stat().st_mode) == OWNER_RW
    assert stat.S_IMODE(snap.parent.stat().st_mode) & (stat.S_IRWXG | stat.S_IRWXO) == 0
    doc = json.loads(snap.read_bytes())
    assert doc["health"][0]["name"] == "codex"
    assert SENTINEL.encode() not in out + err
    assert b"Traceback" not in err


def test_module_entry_run_stops_on_sigterm_with_unresponsive_server(tmp_path, child_env):
    """`python -m fafo.cli run` against a socket that accepts and never replies."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept_loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                held.append(srv.accept()[0])
            except OSError:
                pass

    th = threading.Thread(target=accept_loop, daemon=True)
    th.start()
    snap = tmp_path / "snap" / "snapshot.json"
    cfgp = conf(tmp_path, "ollama")
    cfgp.write_text(
        cfgp.read_text().replace("http://ollama.example.invalid:11434", f"http://127.0.0.1:{port}")
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "fafo.cli", "run", "--config", str(cfgp), "--snapshot", str(snap)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env,
    )
    try:
        wait_for(snap, proc)
        time.sleep(0.5)
        rc, took = stop_and_time(proc, signal.SIGTERM)
        _, err = proc.communicate()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        stop.set()
        th.join(timeout=2)
        for c in held:
            c.close()
        srv.close()
    assert rc == 0
    assert took < 2.0
    assert b"Traceback" not in err


def test_module_entry_missing_config_exit_2(tmp_path):
    r = subprocess.run(
        [sys.executable, "-m", "fafo.cli", "run", "--config", str(tmp_path / "missing.toml")],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert r.returncode == 2
    assert b"Traceback" not in r.stderr
    assert b"not found" in r.stderr


def test_doctor_with_no_config_file_never_raises_config_error(capsys, monkeypatch):
    monkeypatch.setattr(cli, "DOCTOR_TIMEOUT_S", 5.0)
    f = {n: (lambda s, c, e, n=n: Stub(n)) for n in ("host", "claude", "codex", "gemini")}
    code, out, err = run_main(["doctor"], f, capsys)
    assert code == 0
    assert [ln.split()[0] for ln in out.splitlines()] == ["host", "claude", "codex", "gemini"]
    assert "config error" not in err


def test_module_doctor_with_no_config_real_sources_exit_matches_health(tmp_path, child_env):
    env = dict(child_env, HOME=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
    r = subprocess.run(
        [sys.executable, "-m", "fafo.cli", "doctor"],
        capture_output=True,
        check=False,
        timeout=60,
        env=env,
    )
    lines = r.stdout.decode().splitlines()
    assert [ln.split()[0] for ln in lines] == ["host", "claude", "codex", "gemini"]
    health = {ln.split()[1] for ln in lines}
    assert health <= {"ok", "absent", "down", "stale"}
    assert r.returncode == (0 if health <= {"ok", "absent"} else 1)
    assert b"config error" not in r.stderr and SENTINEL.encode() not in r.stdout + r.stderr
