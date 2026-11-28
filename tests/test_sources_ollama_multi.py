"""Several Ollama servers in one source: per-server health, backoff and never-vanish rows."""

from __future__ import annotations

import json
import logging
import threading

import pytest

from fafo.config import SourceCfg
from fafo.source import Ctx
from fafo.sources.ollama import HttpResponse, OllamaSource, ServerSpec

A = "http://a.example.invalid:11434"
B = "http://b.example.invalid:11434"


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Net:
    """Fake HttpGet keyed by base URL. A base in `down` raises; `models` maps base to names."""

    def __init__(self, models: dict[str, list[str]]) -> None:
        self.models = models
        self.down: set[str] = set()
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, url: str, timeout: float, max_bytes: int) -> HttpResponse:
        base = url.rsplit("/api/", 1)[0]
        with self.lock:
            self.calls.append(url)
        if base in self.down:
            raise OSError("connection refused")
        if url.endswith("/api/version"):
            return HttpResponse(200, b'{"version":"0"}')
        body = {"models": [{"name": n} for n in self.models[base]]}
        return HttpResponse(200, json.dumps(body).encode())


def make(net: Net, clock: Clock, specs=None):
    specs = specs or [ServerSpec("alpha", A), ServerSpec("beta", B)]
    src = OllamaSource(servers=specs, http=net)
    ctx = Ctx(cfg=SourceCfg(interval_s=2.0), clock=clock, log=logging.getLogger("t"))
    return src, ctx


def names(res):
    return {s.name: [m.name for m in s.models] for s in res.llms}


def test_two_servers_one_result_each_labeled():
    net, clock = Net({A: ["m-a"], B: ["m-b"]}), Clock()
    src, ctx = make(net, clock)
    res = src.collect(ctx)
    assert names(res) == {"alpha": ["m-a"], "beta": ["m-b"]}
    assert all(s.reachable and s.last_ok == 1000.0 for s in res.llms)
    assert res.llm is res.llms[0]
    assert res.status is None


def test_one_dead_server_keeps_the_other_fresh_and_the_dead_one_stale():
    net, clock = Net({A: ["m-a"], B: ["m-b"]}), Clock()
    src, ctx = make(net, clock)
    src.collect(ctx)
    net.down.add(B)
    net.models[A] = ["m-a2"]
    clock.t = 1010.0
    res = src.collect(ctx)
    by = {s.name: s for s in res.llms}
    assert [m.name for m in by["alpha"].models] == ["m-a2"]
    assert by["alpha"].reachable and by["alpha"].last_ok == 1010.0
    # the dead server's last rows stay (never vanish), flagged unreachable, old timestamp
    assert [m.name for m in by["beta"].models] == ["m-b"]
    assert not by["beta"].reachable and by["beta"].last_ok == 1000.0
    assert res.status == "stale"


def test_never_reached_server_still_has_a_row():
    net, clock = Net({A: ["m-a"], B: ["m-b"]}), Clock()
    net.down.add(B)
    src, ctx = make(net, clock)
    res = src.collect(ctx)
    by = {s.name: s for s in res.llms}
    assert set(by) == {"alpha", "beta"}
    assert by["beta"].models == [] and not by["beta"].reachable and by["beta"].last_ok == 0.0


def test_dead_server_backs_off_without_slowing_the_other():
    net, clock = Net({A: ["m-a"], B: ["m-b"]}), Clock()
    net.down.add(B)
    src, ctx = make(net, clock)
    src.collect(ctx)
    n_b = sum(1 for c in net.calls if c.startswith(B))
    clock.t += 1.0  # inside the backoff window of beta (interval 2 s, 1 failure -> 4 s)
    src.collect(ctx)
    assert sum(1 for c in net.calls if c.startswith(B)) == n_b  # not retried yet
    assert sum(1 for c in net.calls if c.startswith(A)) == 4  # alpha polled both rounds
    clock.t += 10.0
    src.collect(ctx)
    assert sum(1 for c in net.calls if c.startswith(B)) > n_b  # retried after the delay


def test_recovered_server_is_fresh_again():
    net, clock = Net({A: ["m-a"], B: ["m-b"]}), Clock()
    src, ctx = make(net, clock)
    src.collect(ctx)
    net.down.add(B)
    clock.t = 1010.0
    src.collect(ctx)
    net.down.clear()
    clock.t = 1100.0
    res = src.collect(ctx)
    assert all(s.reachable and s.last_ok == 1100.0 for s in res.llms)


def test_all_servers_down_raises_so_the_collector_keeps_the_last_good_rows():
    net, clock = Net({A: ["m-a"], B: ["m-b"]}), Clock()
    src, ctx = make(net, clock)
    net.down.update({A, B})
    with pytest.raises(OSError):
        src.collect(ctx)


def test_single_url_form_is_one_server_named_ollama():
    net, clock = Net({A: ["m-a"]}), Clock()
    src = OllamaSource(A, http=net)
    res = src.collect(Ctx(cfg=SourceCfg(), clock=clock, log=logging.getLogger("t")))
    assert [s.name for s in res.llms] == ["ollama"]
    assert res.llm is res.llms[0]


def test_server_count_cap_and_bad_url_are_refused_at_build():
    with pytest.raises(ValueError):
        OllamaSource(servers=[ServerSpec(f"s{i}", A) for i in range(9)])
    with pytest.raises(ValueError):
        OllamaSource(servers=[ServerSpec("a", "http://user:hunter2@x.invalid")])
    with pytest.raises(ValueError):
        OllamaSource(servers=[ServerSpec("a", A), ServerSpec("a", B)])
    with pytest.raises(ValueError):
        OllamaSource(servers=[])


# ---- one deadline per poll, daemon workers ------------------------------------------------


import time

from fafo.sources import ollama as ollama_mod


class SlowNet(Net):
    """Each request advances the fake clock by `step` seconds, and records the timeout it got."""

    def __init__(self, models, clock: Clock, step: float) -> None:
        super().__init__(models)
        self.clock, self.step = clock, step
        self.timeouts: list[float] = []

    def __call__(self, url, timeout, max_bytes):
        self.timeouts.append(timeout)
        self.clock.t += self.step
        return super().__call__(url, timeout, max_bytes)


def test_healthy_slowish_server_two_requests_of_2s_succeeds_within_the_poll_budget():
    clock = Clock()
    net = SlowNet({A: ["m-a"]}, clock, 2.0)
    src, ctx = make(net, clock, [ServerSpec("alpha", A)])
    res = src.collect(ctx)  # 2 s + 2 s = 4 s, the shared budget allows it
    assert res.llms[0].reachable and [m.name for m in res.llms[0].models] == ["m-a"]
    # the second request gets only the time that is left, never more than TIMEOUT_S
    assert net.timeouts[0] == ollama_mod.TIMEOUT_S
    assert net.timeouts[1] <= ollama_mod.TIMEOUT_S
    assert ollama_mod.POLL_DEADLINE_S > 4.0


def test_second_request_is_skipped_when_the_poll_deadline_is_spent():
    clock = Clock()
    net = SlowNet({A: ["m-a"], B: ["m-b"]}, clock, 0.0)
    calls = {A: 0}

    def http(url, timeout, max_bytes):
        base = url.rsplit("/api/", 1)[0]
        if base == A:
            calls[A] += 1
            clock.t += ollama_mod.POLL_DEADLINE_S + 1  # the first request eats the whole budget
        return net(url, timeout, max_bytes)

    src = OllamaSource(servers=[ServerSpec("alpha", A), ServerSpec("beta", B)], http=http)
    ctx = Ctx(cfg=SourceCfg(interval_s=2.0), clock=clock, log=logging.getLogger("t"))
    res = src.collect(ctx)
    assert calls[A] == 1  # /api/version was never sent to the server that spent its budget
    by = {s.name: s for s in res.llms}
    assert not by["alpha"].reachable and by["beta"].reachable


def test_minimum_time_for_a_request_is_kept(monkeypatch):
    clock = Clock()
    net = SlowNet({A: ["m"]}, clock, 0.0)
    monkeypatch.setattr(ollama_mod, "POLL_DEADLINE_S", 0.05)
    src, ctx = make(net, clock, [ServerSpec("alpha", A)])
    src.collect(ctx)
    assert net.timeouts[0] >= ollama_mod.MIN_REQUEST_S


class Hang:
    """Net whose /api/version for `base` blocks until released (a hung second request)."""

    def __init__(self, inner: Net, base: str) -> None:
        self.inner, self.base, self.release = inner, base, threading.Event()

    def __call__(self, url, timeout, max_bytes):
        if url == self.base + "/api/version":
            self.release.wait(30)
            raise OSError("released")
        return self.inner(url, timeout, max_bytes)


def fast_budget(monkeypatch):
    monkeypatch.setattr(ollama_mod, "POLL_DEADLINE_S", 0.3)
    monkeypatch.setattr(ollama_mod, "WAIT_MARGIN_S", 0.1)


def test_hung_second_request_fails_only_that_server(monkeypatch):
    fast_budget(monkeypatch)
    clock = Clock()
    hang = Hang(Net({A: ["m-a"], B: ["m-b"]}), B)
    try:
        src, ctx = make(hang, clock)
        t0 = time.monotonic()
        res = src.collect(ctx)
        assert time.monotonic() - t0 < 3.0  # bounded by the poll deadline, not the hang
        by = {s.name: s for s in res.llms}
        assert by["alpha"].reachable and [m.name for m in by["alpha"].models] == ["m-a"]
        assert not by["beta"].reachable and by["beta"].models == []
        assert res.status == "stale"
    finally:
        hang.release.set()


def ours() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("fafo-ollama")]


def test_poll_threads_are_daemon_so_a_hung_poll_cannot_block_exit(monkeypatch):
    fast_budget(monkeypatch)
    hang = Hang(Net({A: ["m-a"], B: ["m-b"]}), B)
    try:
        src, ctx = make(hang, Clock())
        src.collect(ctx)
        live = ours()
        assert live, "the hung poll thread should still be alive"
        assert all(t.daemon for t in live)
    finally:
        hang.release.set()


def test_no_thread_growth_over_50_collects_with_a_permanently_hung_server(monkeypatch):
    fast_budget(monkeypatch)
    clock = Clock()
    hang = Hang(Net({A: ["m-a"], B: ["m-b"]}), B)
    try:
        src, ctx = make(hang, clock)
        for _ in range(50):
            clock.t += 100  # past any backoff
            src.collect(ctx)
        end = time.monotonic() + 1
        while len(ours()) > 1 and time.monotonic() < end:
            time.sleep(0.01)  # a finished poll thread may take a moment to exit
        assert len(ours()) <= 1  # at most the one hung poll; never one more per cycle
    finally:
        hang.release.set()
    deadline = time.monotonic() + 3
    while ours() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ours() == []  # the late result ended the thread; nothing leaked


def test_late_result_from_an_abandoned_poll_is_dropped(monkeypatch):
    fast_budget(monkeypatch)
    clock = Clock()
    gate = threading.Event()
    inner = Net({A: ["fresh"], B: ["m-b"]})
    first = {"n": 0}
    lock = threading.Lock()

    def http(url, timeout, max_bytes):
        if url.startswith(A):
            with lock:
                first["n"] += 1
                mine = first["n"]
            if mine == 1:  # the first poll hangs, then answers late with other rows
                gate.wait(10)
                return HttpResponse(200, b'{"models":[{"name":"late"}]}')
        return inner(url, timeout, max_bytes)

    src, ctx = make(http, clock)
    src.collect(ctx)  # alpha times out
    gate.set()
    deadline = time.monotonic() + 3
    while ours() and time.monotonic() < deadline:
        time.sleep(0.02)  # the abandoned poll finishes; its result must not be used
    clock.t += 100
    res = src.collect(ctx)
    assert [m.name for m in {s.name: s for s in res.llms}["alpha"].models] == ["fresh"]


def test_process_exits_with_a_poll_still_hung():
    import subprocess
    import sys

    script = (
        "import logging, threading\n"
        "from fafo.config import SourceCfg\n"
        "from fafo.source import Ctx\n"
        "from fafo.sources.ollama import OllamaSource, ServerSpec\n"
        "from fafo.sources import ollama\n"
        "ollama.POLL_DEADLINE_S = 0.2\n"
        "ollama.WAIT_MARGIN_S = 0.1\n"
        "never = threading.Event()\n"
        "src = OllamaSource(servers=[ServerSpec('a', 'http://a.example.invalid')],\n"
        "                   http=lambda u, t, m: never.wait(600))\n"
        "ctx = Ctx(cfg=SourceCfg(), clock=lambda: 0.0, log=logging.getLogger('t'))\n"
        "try:\n"
        "    src.collect(ctx)\n"
        "except Exception:\n"
        "    pass\n"
        "print('left-main')\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed argv, our own script, bounded timeout
        [sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=20,
        check=False,
    )
    assert out.returncode == 0 and "left-main" in out.stdout


def test_slowish_healthy_server_in_real_time_is_waited_for_the_whole_poll_budget(monkeypatch):
    monkeypatch.setattr(ollama_mod, "POLL_DEADLINE_S", 1.5)
    monkeypatch.setattr(ollama_mod, "WAIT_MARGIN_S", 0.2)
    inner = Net({A: ["m-a"]})

    def slow(url, timeout, max_bytes):
        time.sleep(0.5)  # two requests of 0.5 s: 1.0 s, longer than one request's share
        return inner(url, timeout, max_bytes)

    src = OllamaSource(servers=[ServerSpec("alpha", A)], http=slow)
    ctx = Ctx(cfg=SourceCfg(), clock=time.time, log=logging.getLogger("t"))
    res = src.collect(ctx)
    assert res.llms[0].reachable and res.status is None
