"""Two readings of the same text: controls split words, or they are removed and glue. The masker
decides on both and keeps the reading that redacts more, so no invisible character can hide a
credential by splitting it, and none can glue a flag to its value."""

from __future__ import annotations

import pytest

from fafo.redact import mask_credentials, masked_text

# Built from pieces, made up: 60 characters of uppercase letters and digits. Split at any
# offset between 22 and 38, both halves are shorter than the 40-character run rule.
CANARY = "A1B2C3D4" * 7 + "E5F6"
ZWSP, ZWJ, LRM, BOM = chr(0x200B), chr(0x200D), chr(0x200E), chr(0xFEFF)
LONE_SURROGATE = chr(0xD800)
SHORT = "Q7X2K9B4"  # a short value: only the flag rule can hide it (no run rule)
INVISIBLES = [ZWSP, ZWJ, LRM, BOM, LONE_SURROGATE]


def _leaks(out: str, canary: str = CANARY) -> bool:
    """A canary character outside the redaction marker is a leak."""
    return bool(set(out.replace("<redacted>", "")) & set(canary))


@pytest.mark.parametrize("ch", INVISIBLES)
@pytest.mark.parametrize("offset", [22, 30, 38])
def test_invisible_inside_a_run_does_not_split_it_below_the_run_rule(ch, offset):
    text = CANARY[:offset] + ch + CANARY[offset:]
    assert not _leaks(masked_text(text, 60))
    assert not _leaks(mask_credentials(text))


@pytest.mark.parametrize("ch", INVISIBLES)
def test_invisible_inside_a_flag_name_does_not_hide_the_flag(ch):
    # "--tok" ZWSP "en" is one word to a reader, "--token". Split, it is not a secret flag.
    assert not _leaks(masked_text("--tok" + ch + "en " + SHORT, 60), SHORT)


@pytest.mark.parametrize("ch", INVISIBLES)
def test_invisible_between_flag_and_value_is_masked(ch):
    assert not _leaks(masked_text("--token" + ch + " " + SHORT, 60), SHORT)
    assert not _leaks(masked_text("--token " + ch + SHORT, 60), SHORT)


@pytest.mark.parametrize("ch", INVISIBLES)
def test_benign_text_with_invisibles_stays_readable(ch):
    assert masked_text("hello" + ch + "world", 60) == "hello world"
    assert masked_text("note" + ch + " here", 60) == "note here"
