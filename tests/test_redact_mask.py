import time

from fafo.redact import mask_credentials

TOKEN = "ghp_" + "a1B2" * 8


def test_masks_known_credential_shapes():
    text = (
        f"tool {TOKEN} https://user:pw@host/x Bearer abcdef token=abc123 "
        "GH_TOKEN=zzz " + "A" * 50
    )
    out = mask_credentials(text)
    for secret in (TOKEN, "user:pw", "abcdef", "abc123", "zzz", "A" * 50):
        assert secret not in out
    assert out.startswith("tool ")


def test_keeps_harmless_text():
    text = "claude --resume /usr/bin/claude exec"
    assert mask_credentials(text) == text


def test_bounded_on_hostile_input():
    t0 = time.monotonic()
    out = mask_credentials("a" * 200_000 + " " + "=" * 50_000 + " token: " + "x" * 100_000)
    assert time.monotonic() - t0 < 2.0
    assert len(out) < 5_000
    assert mask_credentials("") == ""
