"""Control characters and Unicode separators are word breaks for masking, never glue."""

from __future__ import annotations

import pytest

from fafo.redact import mask_credentials, masked_text

# Built from pieces, made up: uppercase letters and digits only, so it shares no character with
# the benign words used below. Eight characters: short, like a hunter2-shaped value, so only
# flag and key handling can mask it (a long run would be masked by the run rule anyway).
CANARY = "Q7X2K9B4"
SEP_CANARY = CANARY
NUL, TAB, CR, LF = chr(0), chr(9), chr(13), chr(10)
NBSP, LS, ESC = chr(0xA0), chr(0x2028), chr(27)


def _leaks(out: str) -> bool:
    """A canary character outside the redaction marker is a leak."""
    return bool(set(out.replace("<redacted>", "")) & set(CANARY))


@pytest.mark.parametrize(
    "text",
    [
        "--token" + NUL + CANARY,
        "--token" + TAB + CANARY,
        "password" + TAB + "=" + TAB + CANARY,
        "password" + NUL + "=" + NUL + CANARY,
        "api_key" + CR + LF + "=" + CR + LF + CANARY,
        "api_key" + NBSP + "=" + LS + CANARY,
        "--token=" + NUL + CANARY,
    ],
)
def test_separator_between_key_and_value_never_glues_the_value(text):
    out = masked_text(text, 60)
    assert "<redacted>" in out
    assert not _leaks(out)


def test_benign_words_with_controls_keep_single_spaces():
    assert masked_text("hello" + TAB + "world" + NUL + "again", 60) == "hello world again"
    assert masked_text("a" + LF + CR + LF + "b", 60) == "a b"
    assert masked_text("x" + NBSP + "y" + LS + "z", 60) == "x y z"


def test_bare_key_then_controls_reads_exactly_like_a_space():
    """A bare secret word followed by a value is not a key-value pair to the masker, with or
    without controls: the controls must act as the space they replace, never glue the words."""
    for sep in (CR + LF, NBSP, LS, NUL):
        assert masked_text("api_key" + sep + CANARY, 60) == masked_text("api_key " + CANARY, 60)


def test_cap_is_applied_after_masking_and_separation():
    out = masked_text((NUL + " ").join(["word"] * 40), 9)  # spaces too: no glued run
    assert out == "word word"


@pytest.mark.parametrize(
    "text",
    [
        "--token" + NUL + CANARY,
        "--token" + TAB + CANARY,
        "password" + TAB + "=" + TAB + CANARY,
    ],
)
def test_direct_callers_of_mask_credentials_see_controls_as_breaks(text):
    """Sources that call mask_credentials on their own text get the same word breaks."""
    out = mask_credentials(text)
    assert "<redacted>" in out and not _leaks(out)


# -- a secret key followed by a separator-led value, with any spacing -----------------------
@pytest.mark.parametrize(
    "text",
    [
        "password" + " " + "=" + SEP_CANARY,
        "token" + " " + ":" + SEP_CANARY,
        "secret" + " " + "=" + " " + SEP_CANARY,
        "key" + CR + LF + "=" + TAB + SEP_CANARY,
        "key" + NUL + "=" + SEP_CANARY,
        "password" + " " + "-" + SEP_CANARY,
    ],
)
def test_secret_key_then_separator_led_value_is_masked(text):
    out = masked_text(text, 60)
    assert "<redacted>" in out and not _leaks(out)


def test_negative_prose_after_a_key_word_is_not_masked():
    """Prose after a key word is shown, as before: only separator-led values are masked."""
    assert masked_text("token is abc123", 60) == "token is abc123"
    assert masked_text("hello =world", 60) == "hello =world"
