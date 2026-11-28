"""Codex quota source: windows from the newest rollout tail, privacy and hostile-input guards.

Every fixture is built by hand with made-up numbers; no real rollout content is used.
"""

import json
import logging
import os
import stat
from datetime import UTC
from pathlib import Path

import pytest

from fafo.config import SourceCfg
from fafo.registry import build
from fafo.source import Ctx
from fafo.sources import quota_codex
from fafo.sources.quota_codex import CodexQuotaSource

NOW = 1_800_000_000.0
# A credential-shaped canary that must never reach any output.
CANARY = "tok" + "-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def ctx(now=NOW):
    return Ctx(cfg=SourceCfg(), clock=lambda: now, log=logging.getLogger("fafo.test.cq"))


def iso(epoch):
    from datetime import datetime

    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%S.123Z")


def rec(ts, p=(300, 42.5, NOW + 7200), s=(10080, 7, NOW + 86400)):
    def win(w):
        if w is None:
            return None
        return {"window_minutes": w[0], "used_percent": w[1], "resets_at": int(w[2])}

    return json.dumps(
        {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": {"primary": win(p), "secondary": win(s)}},
        }
    )


def text_line(text):
    return json.dumps({"timestamp": iso(NOW), "payload": {"type": "message", "text": text}})


def make(home, lines, rel="sessions/2026/11/27/rollout-2026-11-27T10-00-00-abc.jsonl", mtime=None):
    path = Path(home) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def run(home, stale=1800, now=NOW):
    return CodexQuotaSource(home, stale).collect(ctx(now))


def test_fresh_rollout_gives_both_windows(tmp_path):
    make(tmp_path, [text_line("hello"), rec(iso(NOW - 60))])
    res = run(tmp_path)
    q = res.quota["codex"]
    assert res.status is None
    assert q["5h"] == 42.5 and q["wk"] == 7.0
    assert q["5h_resets_in_s"] == 7200 and q["wk_resets_in_s"] == 86400
    assert q["stale"] == 0
    assert q["as_of"] == int(NOW - 60)


def test_last_record_wins_and_null_window_is_omitted(tmp_path):
    make(tmp_path, [rec(iso(NOW - 600)), rec(iso(NOW - 30), p=(300, 90, NOW + 10), s=None)])
    q = run(tmp_path).quota["codex"]
    assert q["5h"] == 90.0 and "wk" not in q


def test_stale_keeps_original_as_of(tmp_path):
    make(tmp_path, [rec(iso(NOW - 4000))])
    q = run(tmp_path).quota["codex"]
    assert q["stale"] == 1
    assert q["as_of"] == int(NOW - 4000)
    assert q["5h"] == 42.5  # dims, never vanishes


def test_stale_after_option_is_used(tmp_path):
    make(tmp_path, [rec(iso(NOW - 100))])
    assert run(tmp_path, stale=50).quota["codex"]["stale"] == 1
    assert run(tmp_path, stale=500).quota["codex"]["stale"] == 0


def test_unusual_window_gets_a_minutes_label_and_past_reset_is_dropped(tmp_path):
    make(tmp_path, [rec(iso(NOW), p=(60, 5, NOW - 10), s=None)])
    q = run(tmp_path).quota["codex"]
    assert q["60m"] == 5.0 and "60m_resets_in_s" not in q


def test_missing_home_sessions_and_empty_are_clean_absent(tmp_path, caplog):
    for home in (tmp_path / "nope", tmp_path):
        res = run(home)
        assert res.status == "absent" and res.quota == {}
    (tmp_path / "sessions").mkdir()
    assert run(tmp_path).status == "absent"
    make(tmp_path, ["not json", text_line("no limits here")])
    assert run(tmp_path).status == "absent"


def test_newest_file_wins_and_only_it_is_opened(tmp_path, monkeypatch):
    old = make(tmp_path, [rec(iso(NOW), p=(300, 11, NOW + 5), s=None)],
               rel="sessions/2026/11/26/rollout-old.jsonl", mtime=NOW - 9000)
    new = make(tmp_path, [rec(iso(NOW), p=(300, 77, NOW + 5), s=None)],
               rel="sessions/2026/11/27/rollout-new.jsonl", mtime=NOW - 100)
    other = make(tmp_path, [rec(iso(NOW), p=(300, 33, NOW + 5), s=None)],
                 rel="sessions/2026/11/25/rollout-older.jsonl", mtime=NOW - 99999)
    opened = []
    real = os.open

    def spy(path, flags, *a, **kw):
        opened.append((os.fspath(path), flags))
        return real(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", spy)
    q = run(tmp_path).quota["codex"]
    assert q["5h"] == 77.0
    files = [n for n, f in opened if n.startswith("rollout-")]
    assert files == [new.name], (files, old.name, other.name)


def test_only_the_tail_is_read(tmp_path, monkeypatch):
    big = [text_line("x" * 900) for _ in range(400)]  # about 360 KB of filler
    make(tmp_path, big + [rec(iso(NOW - 5))])
    sizes = []
    real = os.pread

    def spy(fd, n, off):
        sizes.append((n, off))
        return real(fd, n, off)

    monkeypatch.setattr(os, "pread", spy)
    seen_read = []
    real_read = os.read
    monkeypatch.setattr(os, "read", lambda fd, n: seen_read.append(n) or real_read(fd, n))
    assert run(tmp_path).quota["codex"]["5h"] == 42.5
    assert sum(n for n, _ in sizes) <= quota_codex.TAIL_BYTES
    assert max(n for n, _ in sizes) <= quota_codex.TAIL_BYTES
    assert seen_read == []
    size = next(tmp_path.rglob("rollout-*.jsonl")).stat().st_size
    assert size > 4 * quota_codex.TAIL_BYTES
    assert min(off for _, off in sizes) >= size - quota_codex.TAIL_BYTES


def test_half_written_last_line_is_skipped(tmp_path):
    path = make(tmp_path, [rec(iso(NOW - 20), p=(300, 25, NOW + 60), s=None)])
    with open(path, "a") as f:
        f.write(rec(iso(NOW - 1), p=(300, 99, NOW + 60), s=None)[:70])  # no newline, cut short
    assert run(tmp_path).quota["codex"]["5h"] == 25.0


def test_first_partial_line_of_the_tail_is_not_parsed(tmp_path):
    line = rec(iso(NOW - 5), p=(300, 66, NOW + 60), s=None)
    filler = [text_line("y" * 1000) for _ in range(120)]
    path = make(tmp_path, filler + [line])
    # Cut so that the tail begins inside the final record: its head is gone, nothing usable.
    data = path.read_bytes()
    path.write_bytes(data[: len(data) - len(line) // 2 - 1] + b"\n")
    assert run(tmp_path).status == "absent"


# -- hostile content ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        ('{"timestamp":"2026-11-27T10:00:00Z","payload":{"rate_limits":{"primary":'
        '{"window_minutes":300,"used_percent":NaN,"resets_at":1}}}}'),
        '{"payload":{"rate_limits":{"primary":{"window_minutes":300,"used_percent":Infinity}}}}',
        '{"payload":{"rate_limits":{"primary":{"window_minutes":300,"used_percent":' + "9" * 5000 + "}}}}",
        '{"payload":{"rate_limits":{"primary":{"window_minutes":' + "9" * 400 + ',"used_percent":5}}}}',
        '{"payload":{"rate_limits":{"primary":{"window_minutes":true,"used_percent":5}}}}',
        '{"payload":{"rate_limits":{"primary":{"window_minutes":300,"used_percent":"5"}}}}',
        '{"payload":{"rate_limits":{"primary":{"window_minutes":-5,"used_percent":5}}}}',
        '{"payload":{"rate_limits":[1,2,3]}}',
        '{"payload":{"rate_limits":null}}',
        '{"payload":"rate_limits"}',
        '["rate_limits"]',
    ],
)
def test_hostile_numbers_never_raise_or_yield_quota(tmp_path, raw):
    make(tmp_path, [raw])
    res = run(tmp_path)
    assert res.quota == {} and res.status == "absent"


def test_percent_is_clamped_and_resets_bounded(tmp_path):
    far = NOW + 10**12
    make(tmp_path, [rec(iso(NOW), p=(300, 250.0, far), s=(10080, -30, NOW + 5))])
    q = run(tmp_path).quota["codex"]
    assert q["5h"] == 100.0 and "5h_resets_in_s" not in q
    assert q["wk"] == 0.0 and q["wk_resets_in_s"] == 5


def test_future_timestamp_falls_back_to_mtime(tmp_path):
    make(tmp_path, [rec(iso(NOW + 10**7))], mtime=NOW - 40)
    q = run(tmp_path).quota["codex"]
    assert q["as_of"] == int(NOW - 40)


def test_deep_nesting_line_is_skipped(tmp_path):
    deep = '{"payload":{"rate_limits":' + "[" * 3000 + "]" * 3000 + "}}"
    good = rec(iso(NOW - 3))
    make(tmp_path, [good, deep])
    assert run(tmp_path).quota["codex"]["5h"] == 42.5  # the deep line is skipped, older good one wins


def test_one_megabyte_line_never_raises(tmp_path):
    make(tmp_path, [rec(iso(NOW - 3)), '{"payload":{"rate_limits":"' + "z" * 10**6 + '"}}'])
    res = run(tmp_path)
    assert res.status == "absent"  # the tail is wholly inside the huge line


def test_symlink_rollout_is_ignored(tmp_path):
    real = tmp_path / "elsewhere.jsonl"
    real.write_text(rec(iso(NOW)) + "\n")
    d = tmp_path / "sessions" / "2026" / "11" / "27"
    d.mkdir(parents=True)
    os.symlink(real, d / "rollout-link.jsonl")
    assert run(tmp_path).status == "absent"


def test_symlinked_day_directory_is_not_entered(tmp_path):
    target = tmp_path / "outside" / "27"
    target.mkdir(parents=True)
    (target / "rollout-x.jsonl").write_text(rec(iso(NOW)) + "\n")
    base = tmp_path / "sessions" / "2026" / "11"
    base.mkdir(parents=True)
    os.symlink(target, base / "27")
    assert run(tmp_path).status == "absent"


def test_fifo_rollout_does_not_hang(tmp_path):
    d = tmp_path / "sessions" / "2026" / "11" / "27"
    d.mkdir(parents=True)
    os.mkfifo(d / "rollout-fifo.jsonl")
    assert run(tmp_path).status == "absent"


def test_directory_named_like_a_rollout_is_not_a_candidate(tmp_path):
    d = tmp_path / "sessions" / "2026" / "11" / "27"
    (d / "rollout-dir.jsonl").mkdir(parents=True)
    (d / "rollout-dir.jsonl" / "rollout-inner.jsonl").write_text(rec(iso(NOW)) + "\n")
    assert run(tmp_path).status == "absent"


def test_fifo_swapped_in_after_listing_is_refused_by_fstat(tmp_path, monkeypatch):
    make(tmp_path, [rec(iso(NOW))])
    real = os.fstat
    monkeypatch.setattr(
        os, "fstat", lambda fd: _fake(real(fd), mode=stat.S_IFIFO | 0o600)
    )
    assert run(tmp_path).status == "absent"


def test_wrong_owner_is_refused(tmp_path, monkeypatch):
    make(tmp_path, [rec(iso(NOW))])
    monkeypatch.setattr(quota_codex, "_euid", lambda: os.geteuid() + 1)
    assert run(tmp_path).status == "absent"
    monkeypatch.undo()
    real = os.fstat
    monkeypatch.setattr(os, "fstat", lambda fd: _fake(real(fd), uid=os.geteuid() + 7))
    assert run(tmp_path).status == "absent"


def _fake(st, mode=None, uid=None):
    class S:
        st_mode = st.st_mode if mode is None else mode
        st_uid = st.st_uid if uid is None else uid
        st_size = st.st_size
        st_mtime = st.st_mtime

    return S()


# -- privacy ---------------------------------------------------------------------------------


def test_no_value_appears_in_logs(tmp_path, caplog):
    make(tmp_path, [text_line(CANARY), rec(iso(NOW - 1), p=(300, 61.3125, NOW + 4321), s=None)])
    with caplog.at_level(logging.DEBUG):
        run(tmp_path)
        run(tmp_path / "missing")
        make(tmp_path / "b", [text_line(CANARY), "garbage " + CANARY])
        run(tmp_path / "b")
    text = caplog.text
    for needle in (CANARY, "61.3125", "4321", str(tmp_path)):
        assert needle not in text


def test_canary_in_transcript_never_reaches_any_output(tmp_path, caplog):
    lines = [
        text_line(CANARY),
        rec(iso(NOW - 2)),
        json.dumps({"timestamp": iso(NOW), "payload": {"rate_limits": {"primary": {
            "window_minutes": 300, "used_percent": 12, "resets_at": int(NOW + 99),
            "note": CANARY}, "plan_type": CANARY}}}),
    ]
    make(tmp_path, lines)
    with caplog.at_level(logging.DEBUG):
        res = run(tmp_path)
    dump = json.dumps(res.quota) + repr(res) + caplog.text
    assert CANARY not in dump
    assert set(res.quota["codex"]) <= {"5h", "wk", "5h_resets_in_s", "wk_resets_in_s", "as_of", "stale"}
    assert all(type(v) in (int, float) for v in res.quota["codex"].values())


# -- wiring ----------------------------------------------------------------------------------


def _cfg(tmp_path, body):
    from fafo.config import load_config

    f = tmp_path / "wire.toml"
    f.write_text("[sources.codex_quota]\nenabled = true\n" + body)
    return load_config(f)


def test_registry_builds_it_with_defaults_and_options(tmp_path):
    cfg = _cfg(tmp_path, f'codex_home = "{tmp_path}"\nstale_after_s = 60\n')
    built = [s for s in build(cfg, {}).sources if s.name == "codex_quota"]
    assert built[0].codex_home == tmp_path and built[0].stale_after_s == 60
    plain = _cfg(tmp_path, "")
    got = [s for s in build(plain, {"CODEX_HOME": str(tmp_path)}).sources if s.name == "codex_quota"]
    assert got[0].codex_home == tmp_path
    home = next(s for s in build(plain, {}).sources if s.name == "codex_quota")
    assert home.codex_home == Path(os.path.expanduser("~/.codex"))
    assert home.stale_after_s == 1800


def test_config_parses_options_and_refuses_bad_ones(tmp_path):
    from fafo.config import ConfigError, load_config

    f = tmp_path / "c.toml"
    f.write_text('[sources.codex_quota]\nenabled = true\ncodex_home = "~/x"\nstale_after_s = 90\n')
    assert load_config(f).sources["codex_quota"].options == {"codex_home": "~/x", "stale_after_s": 90}
    for bad in ("stale_after_s = 0", "stale_after_s = -1", 'stale_after_s = "5"', "codex_home = 5", "bogus = 1"):
        f.write_text(f"[sources.codex_quota]\n{bad}\n")
        with pytest.raises(ConfigError):
            load_config(f)


def test_off_by_default():
    from fafo.config import load_config

    assert load_config(None).sources["codex_quota"].enabled is False
