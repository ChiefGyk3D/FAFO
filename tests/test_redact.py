import json
from pathlib import Path

import pytest

from fafo.redact import redact_argv, sanitize_text

FIXTURE = Path(__file__).parent / "fixtures" / "argv_secrets.json"
CASES = json.loads(FIXTURE.read_text())
ESC = "\x1b"
BEL = "\x07"


def _value_flags(case):
    """Fixture cases treat every shown dash-prefixed token as a flag taking a value."""
    return frozenset(a for a in case["show"] if a.startswith("-"))


def test_fixture_collected():
    assert len(CASES) >= 10


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_secret_never_in_output(case):
    out = redact_argv(case["argv"], set(case["show"]), value_flags=_value_flags(case))
    assert case["secrets"], "fixture case must name a secret"
    for secret in case["secrets"]:
        assert secret not in out, f"{secret!r} leaked in {out!r}"


def test_guard_bites_on_naive_join():
    """Negative case: a naive join leaks in every case, so the assertion is meaningful."""
    for case in CASES:
        naive = " ".join(case["argv"])
        assert any(s in naive for s in case["secrets"]), case["name"]


def test_exe_basename_and_shown_args_kept():
    out = redact_argv(["/usr/bin/codex", "exec", "--model", "gpt-5", "do the thing"],
                      {"exec", "--model"}, value_flags=frozenset({"--model"}))
    assert out == "codex exec --model gpt-5 <arg>"


def test_ssh_identity_path_kept():
    out = redact_argv(["ssh", "-i", "/path/key", "host"], {"-i"}, value_flags=frozenset({"-i"}))
    assert out == "ssh -i /path/key <arg>"


def test_shown_flag_equals_form_kept():
    assert redact_argv(["codex", "--model=gpt-5"], {"--model"}, value_flags=frozenset({"--model"})) == "codex --model=gpt-5"


def test_env_prefix_not_shown():
    assert redact_argv(["GH_TOKEN=abc", "cmd"], set()) == "<env> cmd"


def test_empty_argv():
    assert redact_argv([], {"x"}) == ""


def test_very_long_argv_bounded():
    argv = ["tool", *["--token=abc123"] * 100_000]
    out = redact_argv(argv, set())
    assert len(out) <= 1000
    assert "abc123" not in out


def test_one_huge_arg_bounded():
    out = redact_argv(["tool", "x" * 5_000_000], set())
    assert out == "tool <arg>"


def test_non_str_and_surrogate_items_safe():
    bad = b"\xff\xfe--token=zzz".decode("utf-8", "surrogateescape")
    mixed: list = ["tool", bad, b"raw", None, 5]
    out = redact_argv(mixed, {bad})
    assert "zzz" not in out
    out.encode("utf-8")  # must be encodable


def test_control_chars_in_shown_args_stripped():
    out = redact_argv(["tool", "--model", f"a{ESC}[31mb"], {"--model"},
                      value_flags=frozenset({"--model"}))
    assert ESC not in out


def test_sanitize_osc_bel():
    assert sanitize_text(f"{ESC}]0;pwn{BEL}ok") == "ok"


def test_sanitize_osc_st():
    assert sanitize_text(f"{ESC}]8;;http://x{ESC}\\link") == "link"


def test_sanitize_csi():
    assert sanitize_text(f"{ESC}[31;1mred{ESC}[0m") == "red"


def test_sanitize_unterminated_osc_dropped_to_end():
    assert sanitize_text(f"ok{ESC}]0;never ends") == "ok"


def test_sanitize_unterminated_csi_and_lone_esc():
    assert sanitize_text(f"a{ESC}[31;") == "a"
    assert sanitize_text(f"a{ESC}") == "a"


def test_sanitize_c0_c1_and_8bit_csi():
    assert sanitize_text("a\x00b\x07c\nd\x7fe\x85f\x9b31mg") == "abcdefg"


def test_sanitize_bidi_and_surrogates():
    s = "a" + chr(0x202E) + "b" + b"\xff".decode("utf-8", "surrogateescape")
    assert sanitize_text(s) == "ab"


def test_sanitize_truncates():
    assert sanitize_text("x" * 500) == "x" * 200
    assert sanitize_text("abcdef", 3) == "abc"
    assert sanitize_text("abc", 0) == ""


def test_sanitize_huge_hostile_input():
    s = (ESC + "]") * 200_000 + (ESC + "[") * 200_000
    assert sanitize_text(s) == ""
