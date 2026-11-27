"""args_mode: config validation and plumbing to the process sources."""

from pathlib import Path

import pytest
from test_sources_codex import FakeProc, ctx

from fafo.config import Config, ConfigError, load_config
from fafo.registry import build_sources
from fafo.sources.claude import ClaudeSource
from fafo.sources.codex import CodexSource
from fafo.sources.gemini import GeminiSource


def cfg_from(tmp_path, text):
    p = tmp_path / "c.toml"
    p.write_text(text)
    return load_config(p)


def test_default_is_allowlist():
    assert load_config(None).args_mode == "allowlist"


def test_valid_values(tmp_path):
    assert cfg_from(tmp_path, 'args_mode = "safe"\n').args_mode == "safe"
    assert cfg_from(tmp_path, 'args_mode = "allowlist"\n').args_mode == "allowlist"


@pytest.mark.parametrize("bad", ['"SAFE"', '"verbose"', '""', "1", "true", '["safe"]'])
def test_invalid_values_name_option_without_echo(tmp_path, bad):
    with pytest.raises(ConfigError) as err:
        cfg_from(tmp_path, f"args_mode = {bad}\n")
    msg = str(err.value)
    assert "args_mode" in msg
    assert "verbose" not in msg
    assert "SAFE" not in msg


def test_config_dataclass_default():
    cfg = Config(sources={}, aliases={}, snapshot_path=Path("s.json"), show_args=set())
    assert cfg.args_mode == "allowlist"


def test_registry_plumbs_mode_to_sources(tmp_path):
    cfg = cfg_from(tmp_path, 'args_mode = "safe"\n')
    by = {s.name: s for s in build_sources(cfg, {})}
    assert by["claude"].args_mode == "safe"
    assert by["codex"]._args_mode == "safe"
    assert by["gemini"]._args_mode == "safe"
    by = {s.name: s for s in build_sources(cfg_from(tmp_path, ""), {})}
    assert by["claude"].args_mode == "allowlist"
    assert by["codex"]._args_mode == "allowlist"
    assert by["gemini"]._args_mode == "allowlist"


def test_sources_default_to_allowlist():
    assert CodexSource(lister=list)._args_mode == "allowlist"
    assert GeminiSource(lister=list)._args_mode == "allowlist"
    assert ClaudeSource("/nonexistent-example").args_mode == "allowlist"


ARGV = ["codex", "exec", "--flag", "status", "./tests/run.py"]


def _cmd(source_cls, **kw):
    procs = [FakeProc(10, 1, ARGV)]
    res = source_cls(lister=lambda: list(procs), show_args={"exec"}, **kw).collect(ctx())
    return res.procs[0].cmd


def test_codex_source_output_differs_by_mode():
    assert _cmd(CodexSource) == "codex exec <arg> <arg> <arg>"
    assert _cmd(CodexSource, args_mode="safe") == "codex exec --flag status run.py"
