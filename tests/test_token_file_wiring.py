"""token_file through the registry, the collector health text, `fafo doctor` and `fafo run`."""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from pathlib import Path

import pytest

from fafo import cli, ghapi
from fafo.collector import Collector
from fafo.config import load_config
from fafo.ghapi import GhAuthError, GhError
from fafo.registry import build
from fafo.source import Ctx

TOKEN = "ghs_SENTINELwiringTokenFile0123456789"
OTHER = "ghs_SENTINELwiringRotated9876543210"
ENV = "FAFO_TEST_WIRING_GH"
OWNER_RW = stat.S_IRUSR | stat.S_IWUSR
RUNS = json.dumps({"total_count": 0, "workflow_runs": []}).encode()
OK = (200, {"x-ratelimit-remaining": "4000", "x-ratelimit-reset": "9999999999"}, RUNS)


def token_file(tmp_path: Path, text: str = TOKEN, mode: int = OWNER_RW) -> Path:
    p = tmp_path / "gh-token"
    p.write_text(text, encoding="utf-8")
    os.chmod(p, mode)
    return p


def config(tmp_path: Path, source: str, tf: Path, with_env: bool = False) -> Path:
    names = ("host", "claude", "codex", "gemini", "ollama", "runners", "copilot")
    text = "".join(f"[sources.{n}]\nenabled = false\n" for n in names if n != source)
    text += f'[sources.{source}]\ninterval_s = 0.2\ntimeout_s = 5\nrepos = ["example-org/example-repo"]\n'
    text += f'token_file = "{tf}"\n'
    if with_env:
        text += f'token_env = "{ENV}"\n'
    p = tmp_path / "fafo.toml"
    p.write_text(text, encoding="utf-8")
    return p


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.sent: list[str] = []

    def __call__(self, method, url, headers, timeout):
        self.sent.append(headers["Authorization"])
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


@pytest.mark.parametrize("source", ["runners", "copilot"])
def test_token_file_alone_builds_the_source_with_a_client_and_sends_the_file_token(
    tmp_path, monkeypatch, source
):
    rec = Recorder(OK)
    monkeypatch.setattr(ghapi, "_httpx_http", rec)
    cfg = load_config(config(tmp_path, source, token_file(tmp_path)))
    res = build(cfg, {})  # no environment variable at all
    assert [s.name for s in res.sources] == [source] and res.skipped == {}
    assert res.notes == {}
    src = res.sources[0]
    gh = src.gh if source == "runners" else src._runs_api
    gh.runs("example-org/example-repo", "queued")
    assert rec.sent == [f"Bearer {TOKEN}"]


def test_neither_env_nor_file_keeps_the_existing_missing_token_behavior(tmp_path):
    p = tmp_path / "fafo.toml"
    p.write_text('[sources.copilot]\nrepos = ["o/r"]\ntoken_env = "' + ENV + '"\n', encoding="utf-8")
    res = build(load_config(p), {})
    assert res.skipped["copilot"] == f"environment variable {ENV} is not set"


@pytest.mark.parametrize("source", ["runners", "copilot"])
def test_doctor_reports_a_refused_token_file_as_down_with_the_reason_class_only(
    tmp_path, capsys, monkeypatch, caplog, source
):
    tf = token_file(tmp_path, mode=stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)
    rec = Recorder(OK)
    monkeypatch.setattr(ghapi, "_httpx_http", rec)
    caplog.set_level(logging.DEBUG)
    code = cli.main(["doctor", "--config", str(config(tmp_path, source, tf))])
    out = capsys.readouterr()
    assert code == 1
    assert f"{source} down GhError: token file refused: permissions" in out.out
    assert TOKEN not in out.out + out.err + caplog.text
    assert str(tf) not in out.out + out.err + caplog.text
    assert rec.sent == []


def test_doctor_with_a_good_token_file_is_ok_and_never_prints_the_token(
    tmp_path, capsys, monkeypatch, caplog
):
    rec = Recorder(OK)
    monkeypatch.setattr(ghapi, "_httpx_http", rec)
    caplog.set_level(logging.DEBUG)
    code = cli.main(["doctor", "--config", str(config(tmp_path, "runners", token_file(tmp_path)))])
    out = capsys.readouterr()
    assert code == 0 and rec.sent
    assert TOKEN not in out.out + out.err + caplog.text


def test_doctor_persistent_401_prints_the_fixed_text_only(tmp_path, capsys, monkeypatch, caplog):
    monkeypatch.setattr(ghapi, "_httpx_http", Recorder((401, {}, TOKEN.encode())))
    caplog.set_level(logging.DEBUG)
    code = cli.main(["doctor", "--config", str(config(tmp_path, "copilot", token_file(tmp_path)))])
    out = capsys.readouterr()
    assert code == 1
    assert "copilot down GhAuthError: token expired or rejected" in out.out
    assert TOKEN not in out.out + out.err + caplog.text


@pytest.mark.parametrize("source", ["runners", "copilot"])
def test_collector_health_text_for_expired_token_is_fixed_and_value_free(tmp_path, monkeypatch, source):
    monkeypatch.setattr(ghapi, "_httpx_http", Recorder((401, {}, TOKEN.encode())))
    cfg = load_config(config(tmp_path, source, token_file(tmp_path)))
    src = build(cfg, {}).sources[0]
    col = Collector(cfg, [src], tmp_path / "snap.json", write_views_file=False)
    col._poll(col._states[0])
    h = col.snapshot().health[0]
    assert h.status == "down"
    assert h.error == "GhAuthError: token expired or rejected"
    assert TOKEN not in repr(h)


def test_source_recovers_after_the_file_is_rotated(tmp_path, monkeypatch):
    tf = token_file(tmp_path)
    rec = Recorder((401, {}, b"{}"), (401, {}, b"{}"), OK)
    monkeypatch.setattr(ghapi, "_httpx_http", rec)
    cfg = load_config(config(tmp_path, "runners", tf))
    src = build(cfg, {}).sources[0]
    ctx = Ctx(cfg=cfg.sources["runners"], clock=time.time, log=logging.getLogger("t"))
    with pytest.raises(GhAuthError):
        src.collect(ctx)
    new = tmp_path / "gh-token.new"
    new.write_text(OTHER, encoding="utf-8")
    os.chmod(new, OWNER_RW)
    os.replace(new, tf)
    src.collect(ctx)
    assert rec.sent[-1] == f"Bearer {OTHER}"


def test_run_command_publishes_only_the_fixed_reason_for_a_refused_file(tmp_path):
    """`fafo run` as a real process: the snapshot and the output carry no token and no path."""
    import signal
    import subprocess
    import sys

    tf = token_file(tmp_path, mode=stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    snap = tmp_path / "state" / "snapshot.json"
    cfgp = config(tmp_path, "runners", tf)
    proc = subprocess.Popen(
        [sys.executable, "-m", "fafo.cli", "run", "--config", str(cfgp), "--snapshot", str(snap)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    text = ""
    try:
        end = time.monotonic() + 15
        while time.monotonic() < end and "token file refused" not in text:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
            text = snap.read_text(encoding="utf-8") if snap.exists() else ""
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert "token file refused: permissions" in text
    blob = text + out.decode() + err.decode()
    assert TOKEN not in blob and str(tf) not in blob


def test_gherror_types_are_value_free_subclasses():
    assert issubclass(GhAuthError, GhError)
    assert str(GhAuthError()) == "token expired or rejected"
