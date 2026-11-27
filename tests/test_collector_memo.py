import random

from fafo.collector import (
    MEMO_MAX_ENTRIES,
    MEMO_MAX_KEY_LEN,
    Collector,
    _Memo,
    _scrub_result,
)
from fafo.config import Config, load_config
from fafo.model import Proc
from fafo.redact import mask_credentials
from fafo.source import SourceResult

ESC = chr(27)
NUL = chr(0)


def cfg_with(aliases: dict[str, str], tmp_path, name: str = "c.toml") -> Config:
    """A real loaded config, so the alias regex is built the way production builds it."""
    lines = "".join(f'"{k}" = "{v}"\n' for k, v in aliases.items())
    f = tmp_path / name
    f.write_text("[aliases]\n" + lines)
    return load_config(f)


def gen_inputs(n: int, seed: int = 7) -> list[str]:
    rnd = random.Random(seed)
    pieces = [
        "claude", "node", "--model", "main", "feature/x", "Build-Host.invalid", "BUILD-HOST.INVALID",
        "build-host.invalid", "10.0.0.5", "token=abc123", "ghp_" + "a" * 30, "Bearer xyz",
        "https://user:pw@example.com/x", "sk-" + "b" * 24, "caf" + chr(233), chr(0x65E5) + chr(0x672C),
        ESC + "[31m", ESC + "]0;title" + chr(7), NUL, chr(9), chr(10), chr(13), "a" * 120,
        "x" * 1500, "pass: hunter2", "KEY = v", "~/src/proj", "--password", "s3cret",
    ]
    out = []
    for _ in range(n):
        k = rnd.randint(1, 6)
        sep = rnd.choice([" ", "", "  ", chr(9)])
        s = sep.join(rnd.choice(pieces) for _ in range(k))
        if rnd.random() < 0.05:
            s = s * rnd.randint(2, 5)  # some past MEMO_MAX_KEY_LEN
        out.append(s)
    out.extend(["", " ", "a"])
    return out


def test_memo_matches_uncached_for_thousands_of_inputs(tmp_path):
    cfg = cfg_with({"build-host.invalid": "alpha", "10.0.0.5": "beta"}, tmp_path)
    memo = _Memo(cfg)
    inputs = gen_inputs(3000)
    assert any(len(s) > MEMO_MAX_KEY_LEN for s in inputs)  # the bypass path is exercised
    for s in inputs:
        res = SourceResult(procs=[Proc(1, 0, None, "a", s, 0.0, 1, 1.0)])
        plain = _scrub_result(res, cfg)
        for _ in range(2):  # second pass is served from the memo
            assert _scrub_result(res, cfg, memo) == plain
        assert memo.text(s) == cfg.alias(plain_text(s))
        assert memo.cmd(s) == mask_credentials(s)
    assert len(memo) > 0


def plain_text(s: str) -> str:
    from fafo.redact import sanitize_text

    return sanitize_text(s, 1000)


def test_kinds_do_not_collide(tmp_path):
    cfg = cfg_with({}, tmp_path)
    memo = _Memo(cfg)
    s = "run --password hunter2 now"
    assert memo.cmd(s) != memo.text(s)
    assert memo.cmd(s) == mask_credentials(s)  # asked again after the other kind: same


def test_result_is_immutable_str_and_cannot_poison_the_memo(tmp_path):
    cfg = cfg_with({}, tmp_path)
    memo = _Memo(cfg)
    out = memo.text("hello")
    assert type(out) is str
    out2 = out + " changed"  # any "mutation" builds a new str
    assert out2 != memo.text("hello") == "hello"


def test_memo_is_bounded(tmp_path):
    memo = _Memo(cfg_with({}, tmp_path))
    for i in range(20_000):
        memo.text(f"distinct-{i}")
        assert len(memo) <= MEMO_MAX_ENTRIES
    assert len(memo) == MEMO_MAX_ENTRIES
    assert memo.text("distinct-19999") == "distinct-19999"  # newest kept
    memo.text("z" * (MEMO_MAX_KEY_LEN + 1))
    assert len(memo) == MEMO_MAX_ENTRIES  # long strings bypass


def test_collectors_do_not_share_results(tmp_path):
    a = Collector(cfg_with({"hostx.invalid": "AAA"}, tmp_path, "a.toml"), [], tmp_path / "a.json")
    b = Collector(cfg_with({"hostx.invalid": "BBB"}, tmp_path, "b.toml"), [], tmp_path / "b.json")
    assert a._memo is not b._memo
    assert a._memo.text("on HostX.invalid") == "on AAA"
    assert b._memo.text("on HostX.invalid") == "on BBB"
    assert a._memo.text("on HostX.invalid") == "on AAA"


def test_collector_uses_memo_for_source_rows(tmp_path):
    cfg = cfg_with({"hostx.invalid": "AAA"}, tmp_path)
    col = Collector(cfg, [], tmp_path / "s.json")
    res = SourceResult(procs=[Proc(1, 0, None, "a", "ssh hostx.invalid --token abc", 0.0, 1, 1.0)])
    out = _scrub_result(res, cfg, col._memo)
    assert "hostx" not in out.procs[0].cmd and "AAA" in out.procs[0].cmd
    assert len(col._memo) > 0
