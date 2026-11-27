import json
import time
from pathlib import Path

import pytest

from fafo.redact import redact_argv

ROUND2 = json.loads((Path(__file__).parent / "fixtures" / "argv_secrets_round2.json").read_text())
ESC = chr(0x1B)
ZW = chr(0x200B)
VF = frozenset({"--model"})


def _value_flags(case):
    return frozenset(a for a in case["show"] if a.startswith("-"))


def test_round2_fixture_collected():
    assert len(ROUND2) >= 20


@pytest.mark.parametrize("case", ROUND2, ids=[c["name"] for c in ROUND2])
def test_round2_secret_never_in_output(case):
    out = redact_argv(case["argv"], set(case["show"]), value_flags=_value_flags(case))
    assert case["secrets"]
    for secret in case["secrets"]:
        assert secret not in out, f"{secret!r} leaked in {out!r}"


def test_zero_width_name_cannot_hide_from_shown_set():
    out = redact_argv(["tool", f"--tok{ZW}en=abc4711"], {"--token"},
                      value_flags=frozenset({"--token"}))
    assert "abc4711" not in out
    assert ZW not in out


def test_plain_path_value_still_kept():
    out = redact_argv(["ssh", "-i", "/home/user/.ssh/id_ed25519"], {"-i"},
                      value_flags=frozenset({"-i"}))
    assert out == "ssh -i /home/user/.ssh/id_ed25519"


def test_innocent_words_not_masked():
    out = redact_argv(["tool", "--side", "left", "--model", "gpt-5"], {"--side", "--model"},
                      value_flags=frozenset({"--side", "--model"}))
    assert out == "tool --side left --model gpt-5"


def test_overlong_shown_value_masked_not_truncated():
    out = redact_argv(["tool", "--model", "a b " * 100], {"--model"}, value_flags=VF)
    assert out == "tool --model <redacted>"


def test_overlong_leading_and_unshown_tokens_safe():
    big = "x" * 1_000
    assert redact_argv([big, big], set()) == "<exe> <arg>"
    assert redact_argv(["A=" + big, "cmd"], set()) == "<env> cmd"


BASE_N = 125_000
SCALE = 8
FLOOR_S = 0.005
MAX_RATIO = 20  # linear is 8, quadratic is 64
CEILING_S = 5.0


def _best_of_3(fn, timer=time.process_time):
    # process_time (CPU seconds of this process) ignores time spent descheduled on a
    # loaded machine, which is what made wall-clock limits flaky.
    best = None
    for _ in range(3):
        start = timer()
        fn()
        took = timer() - start
        best = took if best is None else min(best, took)
    return best


def _assert_scales_linearly(fn_for_size, n, timer=time.process_time):
    """Scale-free check: t(8n)/t(n) stays far below the quadratic ratio of 64.

    The baseline is clamped up to FLOOR_S, so a call faster than the floor is judged
    by t(8n)/FLOOR_S, not by its own (noise-dominated) time. `timer` is injectable so
    the helper itself can be tested deterministically with a fake clock.
    """
    small = max(_best_of_3(fn_for_size(n), timer), FLOOR_S)
    big = _best_of_3(fn_for_size(n * SCALE), timer)
    assert big < CEILING_S, f"{big:.3f}s at 8n exceeds the ceiling"
    assert big / small < MAX_RATIO, f"ratio {big / small:.1f} (small={small:.4f}s big={big:.4f}s)"


ADVERSARIAL = [
    lambda n: "a" * n,
    lambda n: "https://" + "a" * n,
    lambda n: "a" * n + "://",
    lambda n: "key=" * (n // 4),
    lambda n: "bearer" + " " * n,
    lambda n: "token" + "-" * n + "x",
    lambda n: "/" + "a" * 39 + "/" * n,
    lambda n: (ESC + "]") * (n // 2),
]


def _run_redact(tok):
    def go():
        redact_argv(["tool", "--model", tok, tok], {"--model"}, value_flags=VF)
        redact_argv([tok, "--model", tok], {"--model"}, value_flags=VF)
        redact_argv(["tool", "--model=" + tok], {"--model"}, value_flags=VF)

    return go


@pytest.mark.parametrize("shape", ADVERSARIAL, ids=[str(i) for i in range(len(ADVERSARIAL))])
def test_adversarial_token_scales_linearly(shape):
    _assert_scales_linearly(lambda n: _run_redact(shape(n)), BASE_N)


@pytest.mark.parametrize("shape", ADVERSARIAL, ids=[str(i) for i in range(len(ADVERSARIAL))])
def test_adversarial_1mb_token_is_capped(shape):
    tok = shape(1_000_000)
    assert len(tok) >= 900_000  # something real was built
    for argv in (["tool", "--model", tok], [tok, "--model", "x"], ["tool", tok]):
        out = redact_argv(argv, {"--model"}, value_flags=VF)
        assert tok not in out
        assert len(out) < 300
    shown = redact_argv(["tool", "--model", tok], {"--model"}, value_flags=VF)
    assert shown.endswith("<redacted>")
    assert redact_argv(["tool", tok], {"--model"}, value_flags=VF).endswith("<arg>")


class _FakeClock:
    """A timer whose elapsed time is exactly cost(n), with no real CPU involved."""

    def __init__(self, cost):
        self.now = 0.0
        self.cost = cost

    def __call__(self):
        return self.now

    def for_size(self, n):
        def go():
            self.now += self.cost(n)

        return go


def test_scale_check_bites_on_quadratic_work():
    """Negative case, deterministic: time proportional to n squared must fail."""
    clock = _FakeClock(lambda n: 0.01 * (n / 1000) ** 2)  # 0.01 s at n=1000, ratio 64
    with pytest.raises(AssertionError, match="ratio"):
        _assert_scales_linearly(clock.for_size, 1000, timer=clock)


def test_scale_check_passes_linear_work():
    """Positive control, deterministic: time proportional to n must pass (ratio 8)."""
    clock = _FakeClock(lambda n: 0.01 * (n / 1000))
    _assert_scales_linearly(clock.for_size, 1000, timer=clock)


def test_scale_check_enforces_ceiling():
    clock = _FakeClock(lambda n: 0.001 * n)  # linear, but 8n takes 8 s
    with pytest.raises(AssertionError, match="ceiling"):
        _assert_scales_linearly(clock.for_size, 1000, timer=clock)


def test_scale_check_clamps_baseline_to_floor():
    """A baseline below FLOOR_S is replaced by FLOOR_S, and the ratio is judged against it."""
    # t(n)=0.5 ms (clamped to 5 ms). t(8n)=0.2 s: true ratio 400, ratio vs floor 40.
    slow = _FakeClock(lambda n: 0.0005 if n == 1000 else 0.2)
    with pytest.raises(AssertionError, match="ratio 40"):
        _assert_scales_linearly(slow.for_size, 1000, timer=slow)
    # t(n)=0.5 ms, t(8n)=0.05 s: true ratio 100, but vs the floor it is 10, so it passes.
    # This documents the leniency the floor buys against timer noise on tiny baselines.
    ok = _FakeClock(lambda n: 0.0005 if n == 1000 else 0.05)
    _assert_scales_linearly(ok.for_size, 1000, timer=ok)


def test_regexes_themselves_are_bounded():
    """Defense in depth: even if the length cap were bypassed, no regex is quadratic."""
    from fafo.redact import _CREDENTIAL_VALUE, _LONG_RUN

    shapes = (
        lambda n: "a" * n,
        lambda n: "https://" + "a" * n,
        lambda n: "key" + " " * n,
    )
    for shape in shapes:

        def for_size(n, shape=shape):
            body = shape(n)

            def go():
                _CREDENTIAL_VALUE.search(body)
                _LONG_RUN.search(body)

            return go

        _assert_scales_linearly(for_size, 2_500)
