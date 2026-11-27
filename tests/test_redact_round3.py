import pytest

from fafo.redact import MAX_TOKEN_LEN, redact_argv

SP = " "

# Each value is a retained (shown) --env value that must be fully masked.
LEAK_CASES = {
    "12 spaces after equals": "token=" + SP * 12 + "hunter2",
    "12 spaces before equals": "token" + SP * 12 + "=hunter2",
    "12 spaces around colon": "secret" + SP * 12 + ":" + SP * 12 + "hunter2",
    "200 spaces after equals": "token=" + SP * 200 + "hunter2",
    "word name with spaces": "sid" + SP * 40 + "=hunter2",
    "100 char name prefix": "a" * 100 + "token=hunter2",
    "100 char name suffix": "token" + "x" * 100 + "=hunter2",
    "100 char prefix and suffix": "a" * 100 + "token" + "b" * 100 + "=hunter2",
    "bearer with many spaces": "bearer" + SP * 100 + "hunter2",
    "basic with many spaces": "basic" + SP * 100 + "aGh1bnRlcjI=",
    "long url scheme": "h" + "t" * 100 + "ps://u:hunter2@h/x",
    "long url userinfo": "https://" + "u" * 200 + ":hunter2@h/x",
}


@pytest.mark.parametrize("value", list(LEAK_CASES.values()), ids=list(LEAK_CASES))
def test_spacing_and_length_cannot_hide_a_secret(value):
    assert len(value) <= MAX_TOKEN_LEN
    out = redact_argv(["tool", "--env", value], {"--env"}, value_flags=frozenset({"--env"}))
    assert "hunter2" not in out
    assert "aGh1bnRlcjI" not in out
    assert out == "tool --env <redacted>"


def test_leak_cases_collected():
    assert len(LEAK_CASES) >= 10


def test_innocent_spaced_text_is_still_kept():
    out = redact_argv(["tool", "--label", "hello" + SP * 12 + "world"], {"--label"},
                      value_flags=frozenset({"--label"}))
    assert out == "tool --label hello" + SP * 12 + "world"


@pytest.mark.parametrize("name_len", [1, 127, 128, 140, 200, 250, 255])
def test_long_env_name_is_env_not_exe(name_len):
    value = "Zq9" if name_len <= 250 else ""
    word = "A" * name_len + "=" + value
    assert len(word) <= MAX_TOKEN_LEN
    out = redact_argv([word, "python", "run.py"], set())
    assert out == "<env> python <arg>"
    assert "Zq9" not in out


def test_token_with_equals_never_becomes_exe_basename():
    for word in ("A" * 300 + "=hunter2", "x=hunter2/y"):
        out = redact_argv([word, "python"], set())
        assert "hunter2" not in out
        assert out in ("<env> python", "<exe> <arg>")
