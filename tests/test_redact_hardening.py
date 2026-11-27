import json
import time
from pathlib import Path

import pytest

from fafo.redact import redact_argv

ROUND2 = json.loads((Path(__file__).parent / "fixtures" / "argv_secrets_round2.json").read_text())
ESC = chr(0x1B)
ZW = chr(0x200B)


def test_round2_fixture_collected():
    assert len(ROUND2) >= 20


@pytest.mark.parametrize("case", ROUND2, ids=[c["name"] for c in ROUND2])
def test_round2_secret_never_in_output(case):
    out = redact_argv(case["argv"], set(case["show"]))
    assert case["secrets"]
    for secret in case["secrets"]:
        assert secret not in out, f"{secret!r} leaked in {out!r}"


def test_zero_width_name_cannot_hide_from_shown_set():
    out = redact_argv(["tool", f"--tok{ZW}en=abc4711"], {"--token"})
    assert "abc4711" not in out
    assert ZW not in out


def test_plain_path_value_still_kept():
    out = redact_argv(["ssh", "-i", "/home/user/.ssh/id_ed25519"], {"-i"})
    assert out == "ssh -i /home/user/.ssh/id_ed25519"


def test_innocent_words_not_masked():
    out = redact_argv(["tool", "--side", "left", "--model", "gpt-5"], {"--side", "--model"})
    assert out == "tool --side left --model gpt-5"


def test_overlong_shown_value_masked_not_truncated():
    out = redact_argv(["tool", "--model", "a b " * 100], {"--model"})
    assert out == "tool --model <redacted>"


def test_overlong_leading_and_unshown_tokens_safe():
    big = "x" * 1_000
    assert redact_argv([big, big], set()) == "<exe> <arg>"
    assert redact_argv(["A=" + big, "cmd"], set()) == "<env> cmd"


ADVERSARIAL = [
    "a" * 1_000_000,
    "https://" + "a" * 1_000_000,
    "a" * 1_000_000 + "://",
    "key=" * 250_000,
    "bearer" + " " * 1_000_000,
    "token" + "-" * 1_000_000 + "x",
    "/" + "a" * 39 + "/" * 1_000_000,
    (ESC + "]") * 500_000,
]


@pytest.mark.parametrize("tok", ADVERSARIAL, ids=[str(i) for i in range(len(ADVERSARIAL))])
def test_adversarial_1mb_token_is_fast(tok):
    start = time.monotonic()
    redact_argv(["tool", "--model", tok, tok], {"--model"})
    redact_argv([tok, "--model", tok], {"--model"})
    redact_argv(["tool", "--model=" + tok], {"--model"})
    assert time.monotonic() - start < 0.5


def test_regexes_themselves_are_bounded():
    """Defense in depth: even if the length cap were bypassed, no regex is quadratic."""
    from fafo.redact import _CREDENTIAL_VALUE, _LONG_RUN

    for body in ("a" * 20_000, "https://" + "a" * 20_000, "key" + " " * 20_000):
        start = time.monotonic()
        _CREDENTIAL_VALUE.search(body)
        _LONG_RUN.search(body)
        assert time.monotonic() - start < 1.0
