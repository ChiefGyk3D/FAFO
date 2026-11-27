"""Tests for the Ollama source. No live network: a fake HttpGet and loopback servers only."""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from fafo.config import SourceCfg
from fafo.source import Ctx
from fafo.sources.ollama import (
    MAX_BODY,
    MAX_MODELS,
    HttpResponse,
    OllamaSource,
    default_http_get,
)

URL = "http://example.invalid:11434"
FIXTURE = Path(__file__).parent / "fixtures" / "ollama_ps.json"
CTX = Ctx(cfg=SourceCfg(), clock=lambda: 0.0, log=logging.getLogger("t"))
VERSION = b'{"version":"0.0.0"}'


class Fake:
    def __init__(self, ps: bytes | Exception, version: bytes | Exception = VERSION, status=200):
        self.ps, self.version, self.status = ps, version, status
        self.calls: list[tuple[str, float, int]] = []

    def __call__(self, url: str, timeout: float, max_bytes: int) -> HttpResponse:
        self.calls.append((url, timeout, max_bytes))
        item = self.ps if url.endswith("/api/ps") else self.version
        if isinstance(item, Exception):
            raise item
        return HttpResponse(self.status, item)


def collect(fake, **kw):
    return OllamaSource(URL, http=fake, **kw).collect(CTX)


def ps(models) -> bytes:
    return json.dumps({"models": models}).encode()


def test_fixture_mapped():
    fake = Fake(FIXTURE.read_bytes())
    res = collect(fake)
    assert [c[0] for c in fake.calls] == [f"{URL}/api/ps", f"{URL}/api/version"]
    assert all(c[1] == 3.0 and c[2] == MAX_BODY for c in fake.calls)
    m = res.llm.models
    assert [x.name for x in m] == ["example-model:12b", "small-model:4b"]
    assert (m[0].vram, m[0].context) == (10500000000, 131072)
    assert m[1].expires == 1795811700.0  # 2026-11-27T20:35:00Z
    assert abs(m[0].expires - 1795811400.123456) < 1e-3  # 15:30:00.12-05:00
    assert res.llm.queue == 0 and res.llm.inflight == [] and res.llm.gpu == {}
    assert res.status is None


def test_no_models_is_idle_ok():
    res = collect(Fake(ps([])))
    assert res.llm.models == [] and res.status is None


@pytest.mark.parametrize("bad", ["garbage", "", None, 5, "0001-01-01T00:00:00Z", "2026-11-27T10:00:00"])
def test_bad_expiry_is_zero(bad):
    entry = {"name": "m"}
    if bad is not None:
        entry["expires_at"] = bad
    assert collect(Fake(ps([entry]))).llm.models[0].expires == 0.0


def test_expiry_parsed():
    res = collect(Fake(ps([{"name": "m", "expires_at": "2026-11-27T00:00:00+02:00"}])))
    assert res.llm.models[0].expires == 1795730400.0


@pytest.mark.parametrize("exc", [ConnectionError("x"), TimeoutError("x")])
def test_unreachable_and_timeout_raise(exc):
    with pytest.raises(type(exc)):
        collect(Fake(exc))


def test_version_failure_raises():
    with pytest.raises(ConnectionError):
        collect(Fake(ps([]), ConnectionError("x")))


def test_http_error_status_raises():
    with pytest.raises(RuntimeError):
        collect(Fake(b"{}", status=500))


def test_redirect_not_followed_one_call():
    fake = Fake(b"", status=302)
    with pytest.raises(RuntimeError):
        collect(fake)
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pw@example.invalid:11434",
        "http://user@example.invalid",
        "http://:pw@example.invalid",
        "ftp://example.invalid",
        "file:///etc/passwd",
        "http://",
        "example.invalid:11434",
        "http://example.invalid/?a=b",
        "http://example.invalid/#f",
        "http://example.invalid\n/",
    ],
)
def test_bad_url_rejected_at_construction(url):
    with pytest.raises(ValueError) as e:
        OllamaSource(url, http=Fake(b"{}"))
    assert "pw" not in str(e.value)


def test_https_and_trailing_slash_ok():
    fake = Fake(ps([]))
    OllamaSource("https://example.invalid:11434/", http=fake).collect(CTX)
    assert fake.calls[0][0] == "https://example.invalid:11434/api/ps"


def test_five_mb_response_rejected():
    big = b'{"models":[],"pad":"' + b"x" * (5 * 1024 * 1024) + b'"}'
    json.loads(big)  # valid on its own: only the size cap can reject it
    with pytest.raises(ValueError, match="too large"):
        collect(Fake(big))


def test_deep_json_rejected():
    deep = b'{"models":' + b"[" * 5000 + b"]" * 5000 + b"}"
    with pytest.raises(ValueError):
        collect(Fake(deep))


def test_brackets_in_strings_do_not_count_as_depth():
    name = "[" * 500
    res = collect(Fake(ps([{"name": name}])))
    assert len(res.llm.models) == 1


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"models":"x"}', b'{"models":{}}'])
def test_garbage_raises(body):
    with pytest.raises((ValueError, TypeError)):
        collect(Fake(body))


def test_non_dict_entries_and_bad_numbers_skipped_or_zeroed():
    res = collect(
        Fake(ps(["x", 3, {"name": "ok", "size_vram": -5, "context_length": True},
                 {"name": 5}, {"name": "big", "size_vram": 10**30}]))
    )
    names = [m.name for m in res.llm.models]
    assert names == ["ok", "big"]
    assert res.llm.models[0].vram == 0 and res.llm.models[0].context == 0
    assert res.llm.models[1].vram == 0


def test_control_chars_in_name_sanitized_and_capped():
    esc = chr(27)
    name = esc + "]0;pwn" + chr(7) + "evil" + chr(0) + "x" * 500
    res = collect(Fake(ps([{"name": name}])))
    got = res.llm.models[0].name
    assert chr(27) not in got and chr(7) not in got and chr(0) not in got
    assert "pwn" not in got and got.startswith("evil") and len(got) <= 100


def test_model_count_capped():
    res = collect(Fake(ps([{"name": f"m{i}"} for i in range(MAX_MODELS + 40)])))
    assert len(res.llm.models) == MAX_MODELS == 64


# ---- admission log ----


def log(tmp_path, lines, name="adm.log"):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n")
    return p


def ev(event, model="m", pid=1, ts=100.0):
    return json.dumps({"ts": ts, "event": event, "model": model, "session_pid": pid})


def test_log_missing_is_fine(tmp_path):
    res = collect(Fake(ps([])), admission_log=tmp_path / "nope.log")
    assert res.llm.queue == 0 and res.llm.inflight == []


def test_log_none_is_fine():
    res = collect(Fake(ps([])), admission_log=None)
    assert res.llm.queue == 0


def test_log_malformed_is_fine(tmp_path):
    p = tmp_path / "bad.log"
    p.write_bytes(b"\xff\xfe not json\n{\n[1,2]\n" + ev("admit", pid=7).encode() + b"\n")
    res = collect(Fake(ps([])), admission_log=p)
    assert [i.session_pid for i in res.llm.inflight] == [7]


def test_log_directory_and_fifo_are_fine(tmp_path):
    import os

    res = collect(Fake(ps([])), admission_log=tmp_path)
    assert res.llm.queue == 0
    fifo = tmp_path / "f"
    os.mkfifo(fifo)
    res = collect(Fake(ps([])), admission_log=fifo)
    assert res.llm.queue == 0 and res.llm.inflight == []


def test_log_symlink_not_followed(tmp_path):
    real = log(tmp_path, [ev("admit")], "real.log")
    link = tmp_path / "link.log"
    link.symlink_to(real)
    assert collect(Fake(ps([])), admission_log=link).llm.inflight == []


def test_inflight_and_queue_derived(tmp_path):
    p = log(
        tmp_path,
        [
            ev("busy", "a", 1, 1.0),
            ev("admit", "a", 1, 2.0),  # 1 admitted: not queued, in flight
            ev("busy", "b", 2, 3.0),  # still queued
            ev("busy", "c", 3, 4.0),  # still queued
            ev("admit", "d", 4, 5.0),
            ev("release", "d", 4, 6.0),  # done
            ev("evict", "x", None, 7.0),
        ],
    )
    llm = collect(Fake(ps([])), admission_log=p).llm
    assert llm.queue == 2
    assert [(i.model, i.session_pid, i.started) for i in llm.inflight] == [("a", 1, 2.0)]


def test_log_only_tail_read(tmp_path):
    # An old admit far back in a big file is not seen: only the last 200 lines count.
    lines = [ev("admit", "old", 9, 1.0)] + [ev("evict", "x", None, 2.0)] * 300
    p = log(tmp_path, lines)
    assert collect(Fake(ps([])), admission_log=p).llm.inflight == []


def test_log_huge_line_does_not_blow_up(tmp_path):
    p = tmp_path / "big.log"
    p.write_bytes(b"x" * 500_000 + b"\n" + ev("admit", "m", 5).encode() + b"\n")
    res = collect(Fake(ps([])), admission_log=p)
    assert [i.session_pid for i in res.llm.inflight] == [5]


def test_log_model_name_sanitized(tmp_path):
    p = log(tmp_path, [ev("admit", chr(27) + "[31mred" + chr(7), 1)])
    got = collect(Fake(ps([])), admission_log=p).llm.inflight[0].model
    assert chr(27) not in got and chr(7) not in got


# ---- the real default HttpGet against a loopback server ----


class _H(BaseHTTPRequestHandler):
    hits: ClassVar[list[str]] = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        _H.hits.append(self.path)
        if self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", "/target")
            self.end_headers()
        elif self.path == "/big":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"x" * (MAX_BODY * 4))
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")


@pytest.fixture
def server():
    _H.hits = []
    srv = HTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()
    t.join(5)


def test_default_get_ok(server):
    r = default_http_get(server + "/ok", 3.0, MAX_BODY)
    assert (r.status, r.body) == (200, b"{}")


def test_default_get_does_not_follow_redirect(server):
    r = default_http_get(server + "/redir", 3.0, MAX_BODY)
    assert r.status == 302 and _H.hits == ["/redir"]


def test_default_get_caps_body(server):
    with pytest.raises(ValueError):
        default_http_get(server + "/big", 3.0, MAX_BODY)


# ---- round 2: whole-call deadline, pid and ts hardening ----


def test_slow_drip_server_hits_whole_call_deadline():
    stop = threading.Event()

    class Drip(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            try:
                self.send_response(200)
                self.end_headers()
                for _ in range(16):  # bounded: ends by itself even if the client does not
                    if stop.is_set():
                        break
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    stop.wait(0.5)
            except OSError:
                pass

    srv = HTTPServer(("127.0.0.1", 0), Drip)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            default_http_get(f"http://127.0.0.1:{srv.server_address[1]}/x", 2.0, MAX_BODY)
        assert time.monotonic() - start < 2.0 + 1.5
    finally:
        stop.set()
        srv.shutdown()
        srv.server_close()
        t.join(5)
    assert not t.is_alive()


def test_negative_session_pid_treated_as_none(tmp_path):
    p = log(tmp_path, [ev("admit", "m", -5), ev("busy", "q", -1)])
    llm = collect(Fake(ps([])), admission_log=p).llm
    assert [i.session_pid for i in llm.inflight] == [None]
    assert llm.queue == 1


@pytest.mark.parametrize("ts", ["1" + "0" * 400, "1e999", "NaN", "-1", "-Infinity", "5e9"])
def test_bad_ts_skips_record_without_raising(tmp_path, ts):
    bad = '{"ts":' + ts + ',"event":"admit","model":"bad","session_pid":1}'
    assert len(bad) < 4096
    p = log(tmp_path, [bad, ev("admit", "good", 2, 10.0)])
    llm = collect(Fake(ps([])), admission_log=p).llm
    assert [i.model for i in llm.inflight] == ["good"]
