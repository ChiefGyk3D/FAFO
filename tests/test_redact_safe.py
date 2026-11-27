"""Safe mode of redact_argv (args_mode = "safe"): conservative display of command lines.

Every rule has a table case, the hostile fixtures run through both modes, and a generated
property test proves the output alphabet and length bounds.
"""

import json
import random
import re
import time
from pathlib import Path

import pytest
from test_redact_hardening import ADVERSARIAL, _assert_scales_linearly
from test_redact_round3 import LEAK_CASES

from fafo.redact import MAX_TOKEN_LEN, redact_argv

FIXTURES = Path(__file__).parent / "fixtures"
ESC = chr(0x1B)
BEL = chr(7)
ZW = chr(0x200B)
E_ACUTE = chr(0xE9)
SP = " "
CAFE = "caf" + E_ACUTE
LONG41 = "x" * 41
OPT39 = "a" * 39
MODEL_VF = frozenset({"--model"})


def safe(argv, show=(), vf=frozenset()):
    return redact_argv(argv, set(show), value_flags=vf, mode="safe")


# (id, argv, expected output). show_args is empty unless the case says so below.
TABLE = [
    ("python script", ["python3", "script.py", "arg"], "python3 script.py arg"),
    ("python -m", ["python", "-m", "pytest", "tests"], "python -m pytest tests"),
    ("node script", ["node", "app.js"], "node app.js"),
    ("bash -c shows only script", ["bash", "-c", 'curl -H "Authorization: Bearer x" url'],
     "bash -c <script>"),
    ("bash -lc cluster", ["/bin/bash", "-lc", "echo hi"], "bash -lc <script>"),
    ("bash -c hides trailing args", ["bash", "-c", "x", "status", "y"], "bash -c <script>"),
    ("node -e", ["node", "-e", "console.log(1)"], "node -e <script>"),
    ("git commit message", ["git", "commit", "-m", "message with secret"], "git commit -m <arg>"),
    ("ssh key path basename", ["ssh", "-i", "~/.ssh/key", "host"], "ssh -i key host"),
    ("docker env", ["docker", "run", "--rm", "-e", "TOKEN=abc", "image"],
     "docker run --rm -e <arg> image"),
    ("url with userinfo", ["curl", "https://user:pw@host.example.invalid/x"], "curl <arg>"),
    ("url plain", ["curl", "https://host.example.invalid/x"], "curl <arg>"),
    ("mysql glued password", ["mysql", "-psecret"], "mysql <arg>"),
    ("mysql glued password not secret-named", ["mysql", "-phunter2"], "mysql <arg>"),
    ("aws s3 uri and relative path", ["aws", "s3", "cp", "s3://bucket/key", "./file"],
     "aws s3 cp <arg> file"),
    ("pip requirements", ["pip", "install", "-r", "requirements.txt"],
     "pip install -r requirements.txt"),
    ("env words stay env", ["FOO=bar", "BAZ=1", "make", "build"], "<env> <env> make build"),
    ("secret flag then value", ["tool", "--token", "abc"], "tool <arg> <arg>"),
    ("secret flag equals", ["tool", "--token=abc"], "tool <arg>"),
    ("short secret flag then value", ["tool", "-p", "secret"], "tool <arg> <arg>"),
    ("api-key flag then value", ["tool", "--api-key", "abc"], "tool <arg> <arg>"),
    ("follower only skips one", ["tool", "--password", "x", "status"], "tool <arg> <arg> status"),
    ("follower equals form", ["tool", "--password=x", "status"], "tool <arg> status"),
    ("bearer header words", ["tool", "-H", "Authorization:", "Bearer", "abc"],
     "tool -H <arg> <arg> <arg>"),
    ("plain flag", ["tool", "--verbose"], "tool --verbose"),
    ("flag equals value hidden", ["tool", "--level=3"], "tool --level=<arg>"),
    ("bad flag shape", ["tool", "--bad_flag", "x"], "tool <arg> x"),
    ("overlong flag name", ["tool", "--" + "a" * 40], "tool <arg>"),
    ("short cluster", ["tool", "-xvf", "a.tar"], "tool -xvf a.tar"),
    ("absolute path basename", ["tool", "/etc/hosts"], "tool hosts"),
    ("home path basename", ["tool", "~/x"], "tool x"),
    ("path with space", ["tool", "/home/u/my dir/x"], "tool <arg>"),
    ("dotdot relative", ["tool", "../x"], "tool <arg>"),
    ("trailing slash path", ["tool", "/tmp/"], "tool <arg>"),
    ("at sign", ["tool", "a@b"], "tool <arg>"),
    ("equals", ["tool", "a=b"], "tool <arg>"),
    ("dollar", ["tool", "$HOME"], "tool <arg>"),
    ("backtick", ["tool", "`id`"], "tool <arg>"),
    ("quotes", ["tool", "'quoted'"], "tool <arg>"),
    ("known token prefix", ["tool", "ghp_abcdefghijklmnop"], "tool <arg>"),
    ("jwt prefix", ["tool", "eyJhbGciOi"], "tool <arg>"),
    ("base64-ish with digits", ["tool", "aGh1bnRlcjIxMjM0NTY3ODkw"], "tool <arg>"),
    ("over 40 chars", ["tool", LONG41], "tool <arg>"),
    ("39 letters shown", ["tool", OPT39], "tool " + OPT39),
    ("40 letters hit the long-run check", ["tool", "a" * 40], "tool <arg>"),
    ("over token cap", ["tool", "A" * (MAX_TOKEN_LEN + 1)], "tool <arg>"),
    ("non-ascii", ["tool", CAFE], "tool <arg>"),
    ("control char stripped before deciding", ["tool", "a" + BEL + "b"], "tool ab"),
    ("escape sequence stripped", ["tool", ESC + "[31mred"], "tool red"),
    ("zero width stripped", ["tool", "a" + ZW + "b"], "tool ab"),
    ("bare words", ["tool", "status", "run", "tests"], "tool status run tests"),
    ("python module with dots and port", ["python3.11", "-m", "http.server", "8000"],
     "python3.11 -m http.server 8000"),
    ("python bad module", ["python3", "-m", "bad module"], "python3 -m <arg>"),
    ("python script path basename", ["python", "/opt/app/run.py", "--port", "8"],
     "python run.py --port 8"),
    ("python relative script basename", ["python", "scripts/run.py"], "python run.py"),
    ("python url as script", ["python", "-u", "https://x.example.invalid/y"],
     "python -u <arg>"),
    ("uv run", ["uv", "run", "script.py"], "uv run script.py"),
    ("exe with credential shape", ["ghp_abcdefghijklmnop", "x"], "<exe> x"),
    ("empty argv", [], ""),
    ("non-str items", ["tool", None, 5, b"raw"], "tool <arg> <arg> <arg>"),
    ("twelve token cap", ["tool", *["a"] * 20], "tool " + "a " * 10 + "a ..."),
]


def test_table_has_enough_cases():
    assert len(TABLE) >= 40


@pytest.mark.parametrize("argv,want", [(a, w) for _, a, w in TABLE], ids=[c[0] for c in TABLE])
def test_safe_table(argv, want):
    assert safe(argv) == want


def test_every_table_case_is_unchanged_in_default_mode_signature():
    """Default (allowlist) mode is selected when mode is omitted, and shows nothing extra."""
    for _, argv, _ in TABLE:
        assert redact_argv(argv, set()) == redact_argv(argv, set(), mode="allowlist")
    assert redact_argv(["python3", "script.py", "arg"], set()) == "python3 <arg> <arg>"


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        redact_argv(["tool"], set(), mode="verbose")


def test_mode_is_keyword_only():
    with pytest.raises(TypeError):
        redact_argv(["tool"], set(), frozenset(), "safe")


# ---- Named tests that carry the isolated revert proofs ----


def test_positional_shape_check_rejects_url_and_at():
    """Revert proof 1: dropping the safe-shape check on positionals makes this fail."""
    for tok in ("https://u:pw@h.example.invalid/x", "user@host.example.invalid", "a:b", "a=b"):
        assert safe(["tool", tok]) == "tool <arg>", tok


def test_secret_named_flag_follower_is_hidden():
    """Revert proof 2: dropping the follower rule makes this fail."""
    assert safe(["mysql", "-p", "hunter2"]) == "mysql <arg> <arg>"
    assert safe(["tool", "--token", "abc"]) == "tool <arg> <arg>"
    assert safe(["tool", "--auth", "abc", "--client-secret", "def"]) == "tool <arg> <arg> <arg> <arg>"


# ---- Explicitly shown arguments keep their allowlist behavior ----


def test_shown_value_flag_keeps_value_in_safe_mode():
    out = safe(["codex", "--model", "gpt-5", "exec", "go"], show={"--model", "exec"}, vf=MODEL_VF)
    assert out == "codex --model gpt-5 exec go"
    assert safe(["codex", "--model=gpt-5"], show={"--model"}, vf=MODEL_VF) == "codex --model=gpt-5"


def test_shown_value_is_still_masked_in_safe_mode():
    out = safe(["tool", "--model", "ghp_abcdefghijklmnop"], show={"--model"}, vf=MODEL_VF)
    assert out == "tool --model <redacted>"


# ---- Hostile fixtures through both modes ----


def _fixture_cases():
    cases = []
    for name in ("argv_secrets.json", "argv_secrets_round2.json"):
        cases += json.loads((FIXTURES / name).read_text())
    return cases


CASES = _fixture_cases()


def test_fixture_cases_collected():
    assert len(CASES) >= 30


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_fixtures_same_in_allowlist_and_safe_never_leaks(case):
    show = set(case["show"])
    vf = frozenset(a for a in case["show"] if a.startswith("-"))
    default_out = redact_argv(case["argv"], show, value_flags=vf)
    assert redact_argv(case["argv"], show, value_flags=vf, mode="allowlist") == default_out
    out = safe(case["argv"], show=show, vf=vf)
    assert case["secrets"]
    for secret in case["secrets"]:
        assert secret not in default_out
        assert secret not in out, f"{secret!r} leaked in safe mode: {out!r}"


@pytest.mark.parametrize("value", list(LEAK_CASES.values()), ids=list(LEAK_CASES))
def test_round3_leak_cases_in_safe_mode(value):
    vf = frozenset({"--env"})
    for argv in (["tool", "--env", value], ["tool", value], ["tool", "--env=" + value]):
        for show in (set(), {"--env"}):
            out = safe(argv, show=show, vf=vf)
            assert "hunter2" not in out
            assert "aGh1bnRlcjI" not in out


def test_naive_join_leaks_so_the_fixture_assertion_is_meaningful():
    """Negative case: without redaction every fixture would leak."""
    for case in CASES:
        assert any(s in " ".join(case["argv"]) for s in case["secrets"]), case["name"]


# ---- Property test over generated tokens ----

SECRETS = [
    "ghp_abcdefghijklmnopqrstuvwx",
    "sk-abcdefghijklmnopqrstuvwx",
    "AKIAIOSFODNN7EXAMPLE",
    "eyJhbGciOiJIUzI1NiJ9.payload",
    "xoxb-1234-abcdef",
    "hunter2hunter2hunter2hunter2hunter2hunter2",
    "password=hunter2",
    "Bearer hunter2",
    "user:pw@host",
    "aGh1bnRlcjIxMjM0NTY3ODkwYWJjZA",
]
PIECES = ["--token=", "-p", "--", "-", "/", "./", "~/", "x", "a b", "_", "=", ":", "//", "@",
          "", CAFE, ESC + "[0m", BEL, ZW, "$", "`", "'", '"', "key", "status", "run"]
ALPHABET = "abcXYZ019_.+-/=:@ $`'\"~" + CAFE
SAFE_CHARS = re.compile(r"^[A-Za-z0-9_.+<>=-]+$")
SECRET_FLAG_NAME = re.compile(r"token|secret|pass|pwd|auth|bearer|cred|key|private|cookie", re.IGNORECASE)


def _generated_tokens(count, seed):
    rng = random.Random(seed)
    for _ in range(count):
        kind = rng.randrange(3)
        if kind == 0:
            yield "".join(rng.choice(ALPHABET) for _ in range(rng.randrange(1, 70)))
        elif kind == 1:
            yield "".join(rng.choice(PIECES) for _ in range(rng.randrange(1, 6)))
        else:
            parts = [rng.choice(PIECES) for _ in range(rng.randrange(0, 3))]
            yield "".join(parts) + rng.choice(SECRETS) + rng.choice(PIECES)


def test_property_safe_output_is_bounded_and_clean():
    count = 0
    for tok in _generated_tokens(4000, seed=68):
        count += 1
        for argv in (["tool", tok], ["tool", "--token", tok], ["python3", tok, tok]):
            out = safe(argv)
            for secret in SECRETS:
                if secret in tok:
                    assert secret not in out, (tok, out)
            for shown in out.split(SP)[1:]:
                assert shown in ("<arg>", "<redacted>", "<script>", "<env>", "...") or (
                    SAFE_CHARS.match(shown) and len(shown) <= 60
                ), (tok, out)
            assert not any(ord(c) < 32 or ord(c) == 127 for c in out)
    assert count == 4000


def test_property_generator_produces_hostile_tokens():
    """Negative case: the generator really emits secrets, so the property test can bite."""
    toks = list(_generated_tokens(4000, seed=68))
    assert sum(1 for t in toks if any(s in t for s in SECRETS)) > 500
    assert any(ESC in t for t in toks)


def test_secret_named_flags_never_shown_by_name_property():
    rng = random.Random(7)
    names = ["token", "secret", "pass", "pwd", "auth", "bearer", "cred", "key", "private",
             "cookie", "session", "phrase", "jwt", "signature", "p", "pw", "sid", "pin"]
    for _ in range(500):
        name = rng.choice(names)
        flag = ("--" if rng.random() < 0.7 else "-") + name
        out = safe(["tool", flag, "hunter2", "status"])
        assert out == "tool <arg> <arg> status", out


# ---- Bounds and timing ----


def test_safe_mode_adversarial_token_scales_linearly():
    def run(tok):
        def go():
            safe(["python3", tok, tok])
            safe(["tool", "--flag", tok, tok])
            safe(["tool", "--flag=" + tok])

        return go

    for shape in ADVERSARIAL:
        _assert_scales_linearly(lambda n, shape=shape: run(shape(n)), 125_000)


def test_one_megabyte_argv_returns_quickly_and_is_bounded():
    argv = ["python3", *["z" * 255] * 4200]
    assert sum(len(a) for a in argv) > 1_000_000
    start = time.process_time()
    out = safe(argv)
    assert time.process_time() - start < 2.0
    assert out.endswith(" ...")
    assert len(out) < 400
    one = safe(["tool", "x" * 1_000_000])
    assert one == "tool <arg>"


def test_token_count_cap_is_twelve_then_ellipsis():
    out = safe(["tool", *["ok"] * 100])
    assert out.split(SP) == ["tool", *["ok"] * 11, "..."]
    assert safe(["tool", *["ok"] * 11]) == "tool " + SP.join(["ok"] * 11)
