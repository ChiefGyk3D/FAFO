"""Tests for the GitHub REST client. A fake http seam; no network."""

from __future__ import annotations

import json
import logging

import pytest

from fafo.ghapi import GhClient, GhError, RateLimited

TOKEN = "ghs_SENTINELtokenvalue0123456789abcdef"
ENV = "FAFO_TEST_GH_TOKEN"


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeHttp:
    """Scripted responses (the last one repeats); records every request."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers), timeout))
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


def ok(body, **headers):
    h = {"x-ratelimit-remaining": "4000", "x-ratelimit-reset": "1003600"}
    h.update(headers)
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return 200, h, raw


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv(ENV, TOKEN)


def client(http, clock=None, **kw):
    return GhClient(ENV, http=http, clock=clock or Clock(), **kw)


RUNS = {"total_count": 1, "workflow_runs": [{"id": 7, "name": "ci"}]}
JOBS = {"total_count": 1, "jobs": [{"id": 9, "runner_name": "r1"}]}


def test_runs_and_jobs_urls_and_headers():
    http = FakeHttp(ok(RUNS))
    c = client(http)
    assert c.runs("own/repo", "in_progress") == RUNS["workflow_runs"]
    method, url, headers, timeout = http.calls[0]
    assert method == "GET"
    assert url.startswith("https://api.github.com/repos/own/repo/actions/runs?")
    assert "status=in_progress" in url and "per_page=" in url
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert timeout > 0
    http2 = FakeHttp(ok(JOBS))
    assert client(http2).jobs("own/repo", 7) == JOBS["jobs"]
    assert "/repos/own/repo/actions/runs/7/jobs" in http2.calls[0][1]


@pytest.mark.parametrize("repo", ["", "a", "a/b/c", "a/../b", "a b/c", "a/b;x", "a/b?x=1"])
def test_bad_repo_rejected(repo):
    http = FakeHttp(ok(RUNS))
    with pytest.raises(ValueError):
        client(http).runs(repo, "queued")
    assert http.calls == []


def test_bad_status_and_run_id_rejected():
    http = FakeHttp(ok(RUNS))
    with pytest.raises(ValueError):
        client(http).runs("o/r", "in_progress&x=1")
    with pytest.raises(ValueError):
        client(http).jobs("o/r", "7/../x")
    with pytest.raises(ValueError):
        client(http).jobs("o/r", True)
    assert http.calls == []


def test_etag_304_reuses_cached_body_and_sends_if_none_match():
    clock = Clock()
    http = FakeHttp(ok(RUNS, etag='W/"abc"'), (304, {"x-ratelimit-remaining": "4000"}, b""))
    c = client(http, clock, min_interval_s=10)
    first = c.runs("o/r", "queued")
    clock.t += 11
    second = c.runs("o/r", "queued")
    assert first == second == RUNS["workflow_runs"]
    assert len(http.calls) == 2
    assert "If-None-Match" not in http.calls[0][2]
    assert http.calls[1][2]["If-None-Match"] == 'W/"abc"'


def test_poll_floor_serves_cache_without_a_request():
    clock = Clock()
    http = FakeHttp(ok(RUNS, etag='"e"'))
    c = client(http, clock, min_interval_s=10)
    c.runs("o/r", "queued")
    clock.t += 3
    assert c.runs("o/r", "queued") == RUNS["workflow_runs"]
    assert len(http.calls) == 1


def test_poll_interval_header_raises_the_spacing():
    clock = Clock()
    http = FakeHttp(ok(RUNS, etag='"e"', **{"x-poll-interval": "60"}), (304, {}, b""))
    c = client(http, clock, min_interval_s=10)
    c.runs("o/r", "queued")
    clock.t += 30
    c.runs("o/r", "queued")
    assert len(http.calls) == 1
    clock.t += 31
    c.runs("o/r", "queued")
    assert len(http.calls) == 2


def test_429_sets_not_before_and_later_calls_do_not_hit_http():
    clock = Clock()
    http = FakeHttp((429, {"retry-after": "120", "x-ratelimit-remaining": "0"}, b"{}"))
    c = client(http, clock)
    with pytest.raises(RateLimited) as ei:
        c.runs("o/r", "queued")
    assert ei.value.not_before == pytest.approx(clock.t + 120)
    n = len(http.calls)
    clock.t += 60
    with pytest.raises(RateLimited):
        c.jobs("o/r", 1)
    with pytest.raises(RateLimited):
        c.runs("o/r2", "queued")
    assert len(http.calls) == n
    clock.t += 61
    http.responses = [ok(RUNS)]
    assert c.runs("o/r", "queued") == RUNS["workflow_runs"]


def test_403_uses_reset_header_when_no_retry_after():
    clock = Clock()
    http = FakeHttp((403, {"x-ratelimit-reset": str(int(clock.t) + 500)}, b"{}"))
    c = client(http, clock)
    with pytest.raises(RateLimited) as ei:
        c.runs("o/r", "queued")
    assert ei.value.not_before == pytest.approx(clock.t + 500)


def test_low_remaining_backs_off_after_returning_data():
    clock = Clock()
    http = FakeHttp(ok(RUNS, **{"x-ratelimit-remaining": "299", "x-ratelimit-reset": "1000900"}))
    c = client(http, clock)
    assert c.runs("o/r", "queued") == RUNS["workflow_runs"]
    n = len(http.calls)
    with pytest.raises(RateLimited) as ei:
        c.runs("o/r", "in_progress")
    assert ei.value.not_before == pytest.approx(1000900)
    assert len(http.calls) == n


def test_remaining_300_is_not_a_backoff():
    http = FakeHttp(ok(RUNS, **{"x-ratelimit-remaining": "300"}))
    c = client(http)
    c.runs("o/r", "queued")
    c.runs("o/r", "in_progress")
    assert len(http.calls) == 2


def test_redirect_is_an_error_and_not_followed():
    http = FakeHttp((302, {"location": "https://evil.example.invalid/x"}, b""))
    with pytest.raises(GhError):
        client(http).runs("o/r", "queued")
    assert len(http.calls) == 1


def test_oversize_response_rejected():
    big = b'{"workflow_runs": [], "pad": "' + b"x" * (2 * 1024 * 1024) + b'"}'
    with pytest.raises(GhError):
        client(FakeHttp(ok(big))).runs("o/r", "queued")


def test_deep_json_and_wrong_shape_rejected():
    deep = b'{"workflow_runs": ' + b"[" * 200 + b"]" * 200 + b"}"
    with pytest.raises(GhError):
        client(FakeHttp(ok(deep))).runs("o/r", "queued")
    with pytest.raises(GhError):
        client(FakeHttp(ok({"nope": 1}))).runs("o/r", "queued")
    with pytest.raises(GhError):
        client(FakeHttp(ok(b"not json"))).runs("o/r", "queued")


def test_error_status_and_http_exception_are_ghcerror():
    with pytest.raises(GhError):
        client(FakeHttp((500, {}, b"boom"))).runs("o/r", "queued")
    with pytest.raises(GhError):
        client(FakeHttp(OSError("net down"))).runs("o/r", "queued")


def test_missing_token_fails_closed_without_request(monkeypatch):
    monkeypatch.delenv(ENV)
    http = FakeHttp(ok(RUNS))
    with pytest.raises(GhError):
        client(http).runs("o/r", "queued")
    assert http.calls == []


def test_non_https_base_url_rejected():
    with pytest.raises(ValueError):
        GhClient(ENV, base_url="http://api.example.invalid", http=FakeHttp(ok(RUNS)))


def test_token_read_at_call_time(monkeypatch):
    http = FakeHttp(ok(RUNS))
    c = client(http)
    monkeypatch.setenv(ENV, "ghs_rotatedvalue")
    c.runs("o/r", "queued")
    assert http.calls[0][2]["Authorization"] == "Bearer ghs_rotatedvalue"


def test_token_never_in_exceptions_repr_logs_or_instance(caplog):
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    http = FakeHttp(
        (500, {"x-echo": TOKEN}, TOKEN.encode()),
        (429, {"retry-after": "5"}, TOKEN.encode()),
        (302, {"location": "https://x.example.invalid/?t=" + TOKEN}, b""),
        OSError("failed with " + TOKEN),
        ok(b"bad " + TOKEN.encode()),
    )
    c = client(http, clock)
    seen: list[str] = []
    for _ in range(5):
        try:
            c.runs("o/r", "queued")
        except GhError as exc:
            seen += [str(exc), repr(exc)]
        clock.t += 10
    assert len(seen) == 10  # every scripted failure raised
    seen += [repr(c), str(c), c.budget_text(), caplog.text]
    assert all(TOKEN not in s for s in seen)
    assert all(TOKEN not in str(v) for v in vars(c).values())


def test_cache_is_bounded():
    clock = Clock()
    http = FakeHttp(ok(JOBS, etag='"e"'))
    c = client(http, clock, min_interval_s=0)
    for i in range(300):
        c.jobs("o/r", i + 1)
    assert 0 < c.cache_size <= 256


def test_request_budget_counter_and_text():
    http = FakeHttp(ok(RUNS, **{"x-ratelimit-remaining": "4321"}))
    c = client(http)
    assert c.requests_made == 0
    c.runs("o/r", "queued")
    assert c.requests_made == 1
    assert "1" in c.budget_text() and "4321" in c.budget_text()
