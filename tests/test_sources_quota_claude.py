"""claude_quota source: reads a usage cache file that another tool writes."""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from fafo.config import SourceCfg
from fafo.source import Ctx
from fafo.sources import quota_claude
from fafo.sources.quota_claude import ClaudeQuotaSource

NOW = 1_800_000_000.0
HOUR = 3600


def win(label, used, resets, captured):
    return {"label": label, "used_percent": used, "resets_at": resets, "captured_at": captured}


def good(captured=NOW - 60):
    return {
        "windows": [
            win("5h", 41.4, NOW + 2 * HOUR + 600, captured),
            win("weekly", 63, NOW + 3 * 86400, captured),
        ]
    }


@pytest.fixture
def cache(tmp_path):
    d = tmp_path / "cachedir"
    d.mkdir(mode=0o700)
    return d / "usage.json"


def put(path: Path, data, mode=0o600):
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    os.chmod(path, mode)
    return path


def run(path, caplog=None):
    log = logging.getLogger("fafo.source.claude_quota")
    ctx = Ctx(cfg=SourceCfg(), clock=lambda: NOW, log=log)
    return ClaudeQuotaSource(path).collect(ctx)


def err_of(path):
    with pytest.raises(quota_claude.QuotaCacheError) as ei:
        run(path)
    return str(ei.value)


def test_fresh_gives_both_windows(cache):
    put(cache, good())
    res = run(cache)
    assert res.status is None
    q = res.quota["claude"]
    assert set(q) == {"five_hour", "weekly"}
    assert q["five_hour"]["used_pct"] == 41.4
    assert q["five_hour"]["resets_in_s"] == 2 * HOUR + 600
    assert q["weekly"]["used_pct"] == 63.0
    assert q["weekly"]["resets_in_s"] == 3 * 86400
    assert q["five_hour"]["as_of"] == NOW - 60
    assert q["five_hour"]["stale"] is False


def test_stale_keeps_original_as_of(cache):
    old = NOW - 3 * HOUR
    put(cache, good(captured=old))
    q = run(cache).quota["claude"]
    assert q["five_hour"]["as_of"] == old
    assert q["five_hour"]["stale"] is True
    assert q["weekly"]["stale"] is True
    assert q["weekly"]["used_pct"] == 63.0


def test_cutoff_is_thirty_minutes(cache):
    put(cache, good(captured=NOW - quota_claude.STALE_AFTER_S))
    assert run(cache).quota["claude"]["weekly"]["stale"] is False
    put(cache, good(captured=NOW - quota_claude.STALE_AFTER_S - 1))
    assert run(cache).quota["claude"]["weekly"]["stale"] is True
    assert quota_claude.STALE_AFTER_S == 1800


def test_reset_passed_has_no_countdown(cache):
    d = good()
    d["windows"][0]["resets_at"] = NOW - 5
    assert run(put(cache, d)).quota["claude"]["five_hour"]["resets_in_s"] is None


def test_null_reset_ok(cache):
    d = good()
    d["windows"][1]["resets_at"] = None
    assert run(put(cache, d)).quota["claude"]["weekly"]["resets_in_s"] is None


def test_missing_is_absent(tmp_path):
    res = run(tmp_path / "nope" / "usage.json")
    assert res.status == "absent" and res.quota == {}
    d = tmp_path / "there"
    d.mkdir(mode=0o700)
    res = run(d / "usage.json")
    assert res.status == "absent" and res.quota == {}


def test_corrupt_reason_class_only(cache):
    put(cache, b'{"windows": [ SECRET-LOOKING-9f3a')
    msg = err_of(cache)
    assert msg == "corrupt"
    assert "SECRET" not in msg


def test_no_windows_is_absent(cache):
    res = run(put(cache, {"windows": []}))
    assert res.status == "absent" and res.quota == {}


@pytest.mark.parametrize(
    "raw",
    [
        b'{"windows":[{"label":"5h","used_percent":NaN,"resets_at":null,"captured_at":1799999990}]}',
        b'{"windows":[{"label":"5h","used_percent":Infinity,"resets_at":null,"captured_at":1799999990}]}',
        b'{"windows":[{"label":"5h","used_percent":1e999,"resets_at":null,"captured_at":1799999990}]}',
        b'{"windows":[{"label":"5h","used_percent":1,"resets_at":1e999,"captured_at":1799999990}]}',
        b'{"windows":[{"label":"5h","used_percent":1,"resets_at":null,"captured_at":-Infinity}]}',
        b'{"windows":[{"label":"5h","used_percent":' + b"9" * 5000 + b',"resets_at":null,"captured_at":1}]}',
        b'{"windows":[{"label":"5h","used_percent":1,"resets_at":' + b"9" * 400 + b',"captured_at":1}]}',
        b'{"windows":[{"label":"5h","used_percent":true,"resets_at":null,"captured_at":1799999990}]}',
        b'{"windows":[{"label":"5h","used_percent":"41","resets_at":null,"captured_at":1799999990}]}',
        b'{"windows":[{"label":"5h","used_percent":1,"used_percent":2,"resets_at":null,"captured_at":1799999990}]}',
        b'{"windows":[{"label":"5h","used_percent":1,"resets_at":null,"captured_at":1799999990,"x":1}]}',
        b'{"windows":[{"label":"5h","used_percent":1,"resets_at":null}]}',
        (
            b'{"windows":[{"label":"5h","used_percent":1,"resets_at":null,"captured_at":1799999990},'
            b'{"label":"5h","used_percent":2,"resets_at":null,"captured_at":1799999990}]}'
        ),
        b'{"windows":{}}',
        b'{"windows":[],"extra":1}',
        b"[]",
        b'"x"',
        b"\xff\xfe",
        b"",
    ],
)
def test_hostile_content_never_raises_other(cache, raw):
    put(cache, raw)
    try:
        run(cache)
    except quota_claude.QuotaCacheError as exc:
        assert str(exc) in quota_claude.REASONS
    # either an own error with a reason class, or a clean result: nothing else escapes


@pytest.mark.parametrize(
    "raw",
    [
        b'{"windows":[{"label":"5h","used_percent":NaN,"resets_at":null,"captured_at":1799999990}]}',
        b'{"windows":[{"label":"5h","used_percent":1e999,"resets_at":null,"captured_at":1799999990}]}',
        b'{"windows":[{"label":"5h","used_percent":true,"resets_at":null,"captured_at":1799999990}]}',
        b'{"windows":[{"label":"5h","used_percent":1,"used_percent":2,"resets_at":null,"captured_at":1799999990}]}',
        b'{"windows":[{"label":"5h","used_percent":1,"resets_at":null,"captured_at":1799999990,"x":1}]}',
        b"[]",
        b"\xff\xfe",
    ],
)
def test_hostile_content_is_refused(cache, raw):
    put(cache, raw)
    assert err_of(cache) in ("corrupt", "schema")


def test_deep_nesting_refused(cache):
    put(cache, b"[" * 30_000 + b"]" * 30_000)
    assert err_of(cache) == "corrupt"
    put(cache, b'{"windows":' + b"[" * 50 + b"]" * 50 + b"}")
    assert err_of(cache) in ("corrupt", "schema")


def test_brackets_inside_strings_do_not_count(cache):
    # a long run of bracket characters in a string value is not nesting
    s = json.dumps({"windows": [win("[" * 500, 1, None, NOW - 1)]})
    put(cache, s.encode())
    assert run(cache).status == "absent"  # unknown label: ignored, no window left


def test_percent_clamped(cache):
    d = {
        "windows": [
            win("5h", 100.7, None, NOW - 1),
            win("weekly", -0.5, None, NOW - 1),
        ]
    }
    q = run(put(cache, d)).quota["claude"]
    assert q["five_hour"]["used_pct"] == 100.0
    assert q["weekly"]["used_pct"] == 0.0


@pytest.mark.parametrize("bad", [-50, 100000, 10**15, -(10**15), 201, -2])
def test_percent_out_of_sanity_refused(cache, bad):
    d = {"windows": [win("5h", bad, None, NOW - 1)]}
    put(cache, d)
    assert err_of(cache) == "schema"


@pytest.mark.parametrize("bad", [0, -5, 10**12, 2**70, NOW + 90 * 86400])
def test_reset_bounds(cache, bad):
    d = {"windows": [win("5h", 5, bad, NOW - 1)]}
    put(cache, d)
    assert err_of(cache) in ("schema", "corrupt")  # a huge integer is refused at parse time


def test_future_capture_refused(cache):
    put(cache, {"windows": [win("5h", 5, None, NOW + 7 * HOUR)]})
    assert err_of(cache) == "schema"


def test_unknown_label_ignored(cache):
    d = good()
    d["windows"].append(win("monthly", 5, None, NOW - 1))
    assert set(run(put(cache, d)).quota["claude"]) == {"five_hour", "weekly"}


def test_symlink_refused(tmp_path, cache):
    real = put(tmp_path / "real.json", good())
    cache.symlink_to(real)
    assert err_of(cache) == "unreadable"


def test_symlinked_parent_dir_is_the_configured_root(tmp_path):
    real = tmp_path / "realdir"
    real.mkdir(mode=0o700)
    put(real / "usage.json", good())
    link = tmp_path / "linkdir"
    link.symlink_to(real)
    assert run(link / "usage.json").quota["claude"]["weekly"]["used_pct"] == 63.0


def test_fifo_refused_without_hanging(cache):
    os.mkfifo(cache, 0o600)
    assert err_of(cache) == "refused"


def test_directory_refused(cache):
    cache.mkdir(mode=0o700)
    assert err_of(cache) in ("refused", "unreadable")


@pytest.mark.parametrize("mode", [0o660, 0o640, 0o606, 0o666, 0o604, 0o620])
def test_group_or_other_bits_refused(cache, mode):
    put(cache, good(), mode=mode)
    assert err_of(cache) == "refused"


def test_foreign_owner_refused(cache, monkeypatch):
    put(cache, good())
    real = os.geteuid()
    monkeypatch.setattr(quota_claude.os, "geteuid", lambda: real + 1)
    assert err_of(cache) == "refused"


def test_group_writable_parent_refused(cache):
    put(cache, good())
    os.chmod(cache.parent, 0o770)
    assert err_of(cache) == "refused"


def test_oversize_refused(cache):
    pad = b" " * (quota_claude.MAX_BYTES + 1)
    put(cache, json.dumps(good()).encode() + pad)
    assert err_of(cache) == "too_large"


def test_exactly_at_cap_is_read(cache):
    body = json.dumps(good()).encode()
    put(cache, body + b" " * (quota_claude.MAX_BYTES - len(body)))
    assert run(cache).quota["claude"]["weekly"]["used_pct"] == 63.0


def test_mode_bits_have_a_stat_constant_check():
    assert quota_claude.BAD_MODE_BITS == stat.S_IRWXG | stat.S_IRWXO


def test_no_raw_text_in_logs(cache, caplog):
    caplog.set_level(logging.DEBUG)
    put(cache, b'{"windows":[{"label":"5h","used_percent":"LEAKY-VALUE-77"}]}')
    with pytest.raises(quota_claude.QuotaCacheError):
        run(cache)
    put(cache, good(), mode=0o666)
    with pytest.raises(quota_claude.QuotaCacheError):
        run(cache)
    assert "LEAKY" not in caplog.text and str(cache) not in caplog.text


def test_unreadable_perms(cache):
    put(cache, good())
    os.chmod(cache, 0o000)
    if os.geteuid() == 0:
        pytest.skip("root reads anything")
    assert err_of(cache) in ("unreadable", "refused")
