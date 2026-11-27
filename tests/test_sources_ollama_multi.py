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
