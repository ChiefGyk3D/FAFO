import pytest

from fafo.redact import redact_argv

ESC = chr(27)


# ---- Thread C: explicit flag arity ----


def test_boolean_flag_then_secret_positional_is_arg():
    out = redact_argv(["tool", "--verbose", "hunter2"], {"--verbose"})
    assert out == "tool --verbose <arg>"
    assert "hunter2" not in out


def test_boolean_flag_then_shown_word_still_shown():
    out = redact_argv(["tool", "--verbose", "exec"], {"--verbose", "exec"})
    assert out == "tool --verbose exec"


def test_value_flag_keeps_its_value():
    out = redact_argv(["codex", "--model", "gpt-5"], {"--model"}, value_flags=frozenset({"--model"}))
    assert out == "codex --model gpt-5"


def test_value_flag_value_is_still_masked_and_normalized():
    vf = frozenset({"--model"})
    out = redact_argv(["tool", "--model", "ghp_abcdefghijklmnop"], {"--model"}, value_flags=vf)
    assert out == "tool --model <redacted>"
    out = redact_argv(["tool", "--model", "a" + chr(0x200B) + "b"], {"--model"}, value_flags=vf)
    assert out == "tool --model ab"


def test_value_flag_followed_by_another_flag_does_not_consume_it():
    vf = frozenset({"--model"})
    out = redact_argv(["tool", "--model", "--token", "hunter2"], {"--model"}, value_flags=vf)
    assert out == "tool --model <arg> <arg>"
    assert "hunter2" not in out


def test_value_flag_not_shown_is_not_shown():
    out = redact_argv(["tool", "--model", "gpt-5"], set(), value_flags=frozenset({"--model"}))
    assert out == "tool <arg> <arg>"


def test_equals_form_kept_only_for_value_flags():
    assert redact_argv(["tool", "--model=x"], {"--model"}, value_flags=frozenset({"--model"})) \
        == "tool --model=x"
    out = redact_argv(["tool", "--model=hunter2"], {"--model"})
    assert out == "tool <arg>"
    assert "hunter2" not in out


def test_value_flags_is_keyword_only():
    with pytest.raises(TypeError):
        redact_argv(["tool"], {"--model"}, frozenset({"--model"}))


# ---- Thread A and B: named regression tests ----


def test_cookie_header_value_masked_when_header_flag_shown():
    out = redact_argv(["curl", "-H", "Cookie: sessionid=secret123"], {"-H"},
                      value_flags=frozenset({"-H"}))
    assert "secret123" not in out
    assert out == "curl -H <redacted>"


def test_set_cookie_header_value_masked_when_header_flag_shown():
    out = redact_argv(["curl", "-H", "Set-Cookie: sid=secret123; Path=/"], {"-H"},
                      value_flags=frozenset({"-H"}))
    assert "secret123" not in out
    assert out == "curl -H <redacted>"


def test_ansi_split_long_value_masked():
    value = "A" * 24 + ESC + "[31m" + "B" * 20
    out = redact_argv(["tool", "--data", value], {"--data"}, value_flags=frozenset({"--data"}))
    assert "A" * 24 not in out and "B" * 20 not in out
    assert ESC not in out
    assert out == "tool --data <redacted>"
