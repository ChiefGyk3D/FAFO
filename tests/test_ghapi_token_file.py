"""Token file support in the GitHub client: rotation, refusal rules, 401 retry, no leaks."""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from fafo.ghapi import GhAuthError, GhClient, GhError, RateLimited

TOKEN_A = "ghs_SENTINELfileTokenA0123456789"
TOKEN_B = "ghs_SENTINELfileTokenB9876543210"
ENV = "FAFO_TEST_GH_TOKEN_FILE"
ENV_TOKEN = "ghs_SENTINELenvToken5555555555"
OWNER_RW = stat.S_IRUSR | stat.S_IWUSR
RUNS = {"total_count": 0, "workflow_runs": []}


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeHttp:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers), timeout))
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r

    def bearers(self) -> list[str]:
        return [c[2]["Authorization"] for c in self.calls]


def ok(body=RUNS):
    return 200, {"x-ratelimit-remaining": "4000", "x-ratelimit-reset": "1003600"}, json.dumps(body).encode()


def write_token(path: Path, text: str, mode: int = OWNER_RW) -> Path:
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def make(path, http, *, clock=None, **kw) -> GhClient:
    # Time moves by itself in these tests: no spacing cache hides a second request.
    return GhClient(ENV, token_file=str(path), http=http, clock=clock or Clock(), min_interval_s=0, **kw)


def fresh(clock: Clock) -> None:
    clock.t += 1000  # past any cache spacing


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


def test_rotation_by_atomic_rename_is_picked_up_and_unchanged_file_not_reread(tmp_path, monkeypatch):
    tf = write_token(tmp_path / "tok", TOKEN_A + "\n")
    clock = Clock()
    http = FakeHttp(ok())
    c = make(tf, http, clock=clock)
    opened: list[object] = []
    real_open = os.open

    def spy(path, *a, **kw):
        opened.append(path)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("fafo.ghapi.os.open", spy)
    c.runs("o/r", "queued")
    assert len(opened) == 1
    fresh(clock)
    c.runs("o/r", "queued")
    assert len(opened) == 1  # unchanged signature: served from the cache entry
    new = write_token(tmp_path / "tok.new", TOKEN_B)
    os.replace(new, tf)
    fresh(clock)
    c.runs("o/r", "queued")
    assert http.bearers() == [f"Bearer {TOKEN_A}", f"Bearer {TOKEN_A}", f"Bearer {TOKEN_B}"]
    assert len(opened) == 2


def test_rotation_by_rewrite_in_place_is_picked_up(tmp_path):
    tf = write_token(tmp_path / "tok", TOKEN_A)
    clock = Clock()
    http = FakeHttp(ok())
    c = make(tf, http, clock=clock)
    c.runs("o/r", "queued")
    tf.write_text(TOKEN_B + "xx", encoding="utf-8")  # different size
    fresh(clock)
    c.runs("o/r", "queued")
    assert http.bearers()[1] == f"Bearer {TOKEN_B}xx"


@pytest.mark.parametrize(
    "mode",
    [
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH,
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP,
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH,
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IWGRP,
    ],
)
def test_group_or_other_permission_bits_are_refused(tmp_path, mode):
    tf = write_token(tmp_path / "tok", TOKEN_A, mode)
    http = FakeHttp(ok())
    with pytest.raises(GhError, match="^token file refused: permissions$"):
        make(tf, http).runs("o/r", "queued")
    assert http.calls == []


def test_owner_only_modes_are_accepted(tmp_path):
    for mode in (stat.S_IRUSR, OWNER_RW):
        tf = write_token(tmp_path / f"tok{mode}", TOKEN_A, mode)
        http = FakeHttp(ok())
        make(tf, http).runs("o/r", "queued")
        assert http.bearers() == [f"Bearer {TOKEN_A}"]


def test_symlink_is_refused(tmp_path):
    real = write_token(tmp_path / "real", TOKEN_A)
    link = tmp_path / "link"
    link.symlink_to(real)
    http = FakeHttp(ok())
    with pytest.raises(GhError, match="^token file refused: symlink$"):
        make(link, http).runs("o/r", "queued")
    assert http.calls == []


def test_directory_is_refused(tmp_path):
    d = tmp_path / "dir"
    d.mkdir()
    os.chmod(d, stat.S_IRWXU)
    with pytest.raises(GhError, match="^token file refused: not a regular file$"):
        make(d, FakeHttp(ok())).runs("o/r", "queued")


def test_fifo_is_refused_without_blocking(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, OWNER_RW)
    with pytest.raises(GhError, match="^token file refused: not a regular file$"):
        make(fifo, FakeHttp(ok())).runs("o/r", "queued")


def test_empty_and_oversized_files_are_refused(tmp_path):
    empty = write_token(tmp_path / "empty", "")
    with pytest.raises(GhError, match="^token file refused: empty$"):
        make(empty, FakeHttp(ok())).runs("o/r", "queued")
    big = write_token(tmp_path / "big", "a" * 9 * 1024)
    with pytest.raises(GhError, match="^token file refused: too large$"):
        make(big, FakeHttp(ok())).runs("o/r", "queued")


@pytest.mark.parametrize(
    "text",
    ["ghs_bad token", "ghs_bad\ntoken", "ghs_bad$token", "ghs_béd", "ghs_bad\x00", "a" * 2049, "   \n"],
)
def test_invalid_token_text_is_refused_and_never_echoed(tmp_path, text):
    tf = tmp_path / "tok"
    tf.write_bytes(text.encode("utf-8"))
    os.chmod(tf, OWNER_RW)
    http = FakeHttp(ok())
    with pytest.raises(GhError) as ei:
        make(tf, http).runs("o/r", "queued")
    assert str(ei.value) in ("token file invalid", "token file refused: empty")
    assert http.calls == []


def test_wrong_owner_is_refused(tmp_path, monkeypatch):
    tf = write_token(tmp_path / "tok", TOKEN_A)
    other = os.geteuid() + 1
    monkeypatch.setattr("fafo.ghapi.os.geteuid", lambda: other)
    with pytest.raises(GhError, match="^token file refused: wrong owner$"):
        make(tf, FakeHttp(ok())).runs("o/r", "queued")


def test_missing_file_is_value_free_error_when_no_env(tmp_path):
    tf = tmp_path / "secret-dir-name" / "tok"
    http = FakeHttp(ok())
    with pytest.raises(GhError) as ei:
        make(tf, http).runs("o/r", "queued")
    assert "secret-dir-name" not in str(ei.value) + repr(ei.value)
    assert str(ei.value) == "token file not found"
    assert http.calls == []


def test_missing_file_falls_back_to_env_but_refused_file_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, ENV_TOKEN)
    http = FakeHttp(ok())
    make(tmp_path / "absent", http).runs("o/r", "queued")
    assert http.bearers() == [f"Bearer {ENV_TOKEN}"]
    bad = write_token(tmp_path / "bad", TOKEN_A, stat.S_IRUSR | stat.S_IROTH)
    http2 = FakeHttp(ok())
    with pytest.raises(GhError, match="refused"):
        make(bad, http2).runs("o/r", "queued")
    assert http2.calls == []  # a refusal never falls back to the environment


def test_env_only_still_works_and_file_wins_when_both(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, ENV_TOKEN)
    http = FakeHttp(ok())
    GhClient(ENV, http=http, clock=Clock()).runs("o/r", "queued")
    assert http.bearers() == [f"Bearer {ENV_TOKEN}"]
    tf = write_token(tmp_path / "tok", TOKEN_A)
    http2 = FakeHttp(ok())
    make(tf, http2).runs("o/r", "queued")
    assert http2.bearers() == [f"Bearer {TOKEN_A}"]


def test_401_rereads_the_file_and_retries_once_with_the_new_token(tmp_path):
    tf = write_token(tmp_path / "tok", TOKEN_A)
    http = FakeHttp((401, {}, b"{}"), ok())

    def rotate(method, url, headers, timeout, _orig=http):
        r = _orig(method, url, headers, timeout)
        if len(_orig.calls) == 1:  # the issuer refreshes the file after the first rejection
            new = write_token(tmp_path / "tok.new", TOKEN_B)
            os.replace(new, tf)
        return r

    c = make(tf, rotate)
    assert c.runs("o/r", "queued") == []
    assert http.bearers() == [f"Bearer {TOKEN_A}", f"Bearer {TOKEN_B}"]
    assert c.requests_made == 2


def test_401_rereads_a_same_size_in_place_rewrite(tmp_path):
    tf = write_token(tmp_path / "tok", TOKEN_A)
    st = os.stat(tf)
    http = FakeHttp((401, {}, b"{}"), ok())

    def rotate(method, url, headers, timeout, _orig=http):
        r = _orig(method, url, headers, timeout)
        if len(_orig.calls) == 1:
            tf.write_text(TOKEN_B, encoding="utf-8")  # same size, same inode
            os.utime(tf, ns=(st.st_atime_ns, st.st_mtime_ns))  # same mtime: signature unchanged
        return r

    assert len(TOKEN_A) == len(TOKEN_B)
    make(tf, rotate).runs("o/r", "queued")
    assert http.bearers() == [f"Bearer {TOKEN_A}", f"Bearer {TOKEN_B}"]


def test_persistent_401_raises_auth_error_with_fixed_text_after_one_retry(tmp_path):
    tf = write_token(tmp_path / "tok", TOKEN_A)
    http = FakeHttp((401, {}, TOKEN_A.encode()))
    with pytest.raises(GhAuthError) as ei:
        make(tf, http).runs("o/r", "queued")
    assert str(ei.value) == "token expired or rejected"
    assert isinstance(ei.value, GhError)
    assert len(http.calls) == 2  # exactly one retry


def test_403_not_rate_limit_is_auth_and_403_rate_limit_is_not(tmp_path):
    tf = write_token(tmp_path / "tok", TOKEN_A)
    clock = Clock()
    plain = FakeHttp((403, {"x-ratelimit-remaining": "4000", "x-ratelimit-reset": "1003600"}, b"{}"))
    with pytest.raises(GhAuthError):
        make(tf, plain, clock=clock).runs("o/r", "queued")
    assert len(plain.calls) == 2
    for hdr in ({"retry-after": "30"}, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1000500"}):
        http = FakeHttp((403, hdr, b"{}"))
        with pytest.raises(RateLimited):
            make(tf, http, clock=Clock()).runs("o/r", "queued")
        assert len(http.calls) == 1


def test_other_statuses_are_not_retried(tmp_path):
    tf = write_token(tmp_path / "tok", TOKEN_A)
    for status in (404, 500, 422):
        http = FakeHttp((status, {}, b"{}"))
        with pytest.raises(GhError) as ei:
            make(tf, http).runs("o/r", "queued")
        assert not isinstance(ei.value, GhAuthError)
        assert len(http.calls) == 1


def test_env_token_401_also_retries_once(monkeypatch):
    monkeypatch.setenv(ENV, ENV_TOKEN)
    http = FakeHttp((401, {}, b"{}"))
    with pytest.raises(GhAuthError):
        GhClient(ENV, http=http, clock=Clock()).runs("o/r", "queued")
    assert len(http.calls) == 2


def test_sentinel_never_in_any_exception_chain_repr_or_log(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    tf = write_token(tmp_path / "tok", TOKEN_A)
    clock = Clock()
    http = FakeHttp((401, {"x-echo": TOKEN_A}, TOKEN_A.encode()))
    c = make(tf, http, clock=clock)
    seen: list[str] = []

    def collect(exc: BaseException | None) -> None:
        while exc is not None:
            seen.extend([str(exc), repr(exc), *map(str, exc.args)])
            exc = exc.__cause__ or exc.__context__

    with pytest.raises(GhAuthError) as ei:
        c.runs("o/r", "queued")
    collect(ei.value)
    http2 = FakeHttp(OSError("boom " + TOKEN_A))
    with pytest.raises(GhError) as ei2:
        make(tf, http2).runs("o/r", "queued")
    collect(ei2.value)
    bad = write_token(tmp_path / "bad", TOKEN_A, stat.S_IRUSR | stat.S_IROTH)
    with pytest.raises(GhError) as ei3:
        make(bad, FakeHttp(ok())).runs("o/r", "queued")
    collect(ei3.value)
    seen += [repr(c), str(c), c.budget_text(), caplog.text, repr(c._tokens)]
    assert seen and all(TOKEN_A not in s for s in seen)
    assert all(str(tf) not in s for s in seen)
