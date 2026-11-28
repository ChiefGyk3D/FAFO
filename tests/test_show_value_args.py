"""show_value_args: operator-declared value-taking flags whose next token is shown."""

import pytest
from claude_helpers import FakeProc as ClaudeProc
from test_sources_codex import FakeProc, ctx

from fafo.config import DEFAULT_SHOW_VALUE_ARGS, ConfigError, load_config
from fafo.redact import MASK, redact_argv
from fafo.registry import build_sources
from fafo.sources.claude import ClaudeSource
from fafo.sources.codex import CodexSource
from fafo.sources.gemini import GeminiSource


def cfg_from(tmp_path, text):
    p = tmp_path / "c.toml"
    p.write_text(text)
    return load_config(p)


# ---- config parsing ----


def test_defaults_declare_model_and_resume():
    assert set(DEFAULT_SHOW_VALUE_ARGS) == {"--model", "--resume"}
    assert load_config(None).show_value_args == frozenset({"--model", "--resume"})


def test_valid_list(tmp_path):
    cfg = cfg_from(tmp_path, 'show_value_args = ["--model", "-m", "--max-turns"]\n')
    assert cfg.show_value_args == frozenset({"--model", "-m", "--max-turns"})


def test_empty_list_declares_none(tmp_path):
    assert cfg_from(tmp_path, "show_value_args = []\n").show_value_args == frozenset()


@pytest.mark.parametrize(
    "bad",
    [
        '"--model"',
        "1",
        "[1]",
        '["model"]',
        '["---model"]',
        '["--"]',
        '["--mo del"]',
        '["--model=x"]',
        '["--1abc"]',
        '[""]',
        '["--' + "a" * 32 + '"]',
        '["--model\\n"]',
    ],
)
def test_invalid_entries_refused_without_echo(tmp_path, bad):
    with pytest.raises(ConfigError) as ei:
        cfg_from(tmp_path, f"show_value_args = {bad}\n")
    msg = str(ei.value)
    assert "show_value_args" in msg
    assert "model" not in msg.replace("show_value_args", "")
    assert "mo del" not in msg


@pytest.mark.parametrize("name", ["--token", "--api-key", "--password", "--secret", "-p", "--auth"])
def test_secret_named_entry_refused_without_echo(tmp_path, name):
    with pytest.raises(ConfigError) as ei:
        cfg_from(tmp_path, f'show_value_args = ["--model", "{name}"]\n')
    msg = str(ei.value)
    assert "show_value_args" in msg
    assert name not in msg


def test_max_32_entries(tmp_path):
    ok = ", ".join(f'"--f{i}"' for i in range(32))
    assert len(cfg_from(tmp_path, f"show_value_args = [{ok}]\n").show_value_args) == 32
    over = ", ".join(f'"--f{i}"' for i in range(33))
    with pytest.raises(ConfigError, match="show_value_args"):
        cfg_from(tmp_path, f"show_value_args = [{over}]\n")


def test_unknown_top_key_still_refused(tmp_path):
    with pytest.raises(ConfigError):
        cfg_from(tmp_path, 'show_value_arg = ["--model"]\n')


# ---- redaction behavior ----


@pytest.mark.parametrize("mode", ["allowlist", "safe"])
def test_boolean_shown_flag_never_reveals_next_token(mode):
    out = redact_argv(
        ["tool", "--verbose", "hunter2"], {"--verbose"}, value_flags=frozenset(), mode=mode
    )
    assert out == "tool --verbose <arg>"
    assert "hunter2" not in out


@pytest.mark.parametrize("mode", ["allowlist", "safe"])
def test_shown_flag_not_declared_hides_equals_value(mode):
    out = redact_argv(["tool", "--model=gpt-5"], {"--model"}, value_flags=frozenset(), mode=mode)
    assert "gpt-5" not in out


@pytest.mark.parametrize("mode", ["allowlist", "safe"])
def test_declared_value_flag_shows_value(mode):
    out = redact_argv(
        ["tool", "--model", "gpt-5"], {"--model"}, value_flags=frozenset({"--model"}), mode=mode
    )
    assert out == "tool --model gpt-5"


@pytest.mark.parametrize("mode", ["allowlist", "safe"])
def test_declared_value_still_masked_and_capped(mode):
    vf = frozenset({"--model"})
    secret = "ghp_" + "A" * 30
    out = redact_argv(["tool", "--model", secret], {"--model"}, value_flags=vf, mode=mode)
    assert secret not in out
    assert out == f"tool --model {MASK}"
    edge = "a " * 127 + "ab"  # 256 characters, spaces keep it from looking like a long token
    assert len(edge) == 256
    out = redact_argv(["tool", "--model", edge + "b"], {"--model"}, value_flags=vf, mode=mode)
    assert out == f"tool --model {MASK}"
    out = redact_argv(["tool", "--model", edge], {"--model"}, value_flags=vf, mode=mode)
    assert out == "tool --model " + edge


# ---- plumbing to the sources ----

ARGV = ["codex", "exec", "--model", "gpt-5", "--verbose", "hunter2"]


def _cmd(source_cls, vf, mode="allowlist"):
    procs = [FakeProc(10, 1, ARGV)]
    src = source_cls(
        lister=lambda: list(procs),
        show_args={"exec", "--model", "--verbose"},
        value_flags=vf,
        args_mode=mode,
    )
    return src.collect(ctx()).procs[0].cmd


@pytest.mark.parametrize("source_cls", [CodexSource, GeminiSource])
@pytest.mark.parametrize("mode", ["allowlist", "safe"])
def test_codex_gemini_use_the_declared_set(source_cls, mode):
    argv = ["codex" if source_cls is CodexSource else "gemini", *ARGV[1:]]
    procs = [FakeProc(10, 1, argv)]

    def cmd(vf):
        s = source_cls(
            lister=lambda: list(procs),
            show_args={"exec", "--model", "--verbose"},
            value_flags=vf,
            args_mode=mode,
        )
        return s.collect(ctx()).procs[0].cmd

    assert "gpt-5" in cmd(frozenset({"--model"}))
    assert "gpt-5" not in cmd(frozenset())  # declared nothing: --model is boolean
    assert "hunter2" not in cmd(frozenset({"--model"}))
    assert "hunter2" not in cmd(frozenset())


def test_codex_default_value_flags_keep_model_and_resume():
    procs = [FakeProc(10, 1, ["codex", "--model", "gpt-5"])]
    src = CodexSource(lister=lambda: list(procs), show_args={"--model"})
    assert src.collect(ctx()).procs[0].cmd == "codex --model gpt-5"


def test_claude_uses_declared_set(tmp_path):
    argv = ["claude", "--model", "opus", "--verbose", "hunter2"]

    def cmd(vf):
        src = ClaudeSource(
            tmp_path, show_args={"--model", "--verbose"}, value_flags=vf, psutil_mod=None
        )
        row = src._proc_row(ClaudeProc(5, 1, argv), 1)
        return row[0].cmd

    assert cmd(frozenset({"--model"})) == "claude --model opus --verbose <arg>"
    assert cmd(frozenset()) == "claude --model <arg> --verbose <arg>"


def test_registry_plumbs_config_to_sources(tmp_path):
    cfg = cfg_from(tmp_path, 'show_value_args = ["--model", "-m"]\n')
    by = {s.name: s for s in build_sources(cfg, {})}
    want = frozenset({"--model", "-m"})
    assert by["claude"].value_flags == want
    assert by["codex"]._value_flags == want
    assert by["gemini"]._value_flags == want
    by = {s.name: s for s in build_sources(cfg_from(tmp_path, ""), {})}
    assert by["codex"]._value_flags == frozenset({"--model", "--resume"})
