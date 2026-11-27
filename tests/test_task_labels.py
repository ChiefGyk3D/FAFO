"""task_labels option (config, registry) and its wiring into the Claude source."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest
from claude_helpers import LIVE, ctx, make_dir, source

from fafo import transcript
from fafo.config import ConfigError, load_config
from fafo.registry import build_sources

SID_BUSY = "aaaaaaaa-0000-4000-8000-000000000001"
SID_WAIT = "aaaaaaaa-0000-4000-8000-000000000003"
NOW = 1_700_000_500.0


@pytest.fixture(autouse=True)
def _fresh():
    transcript.clear_cache()
    yield
    transcript.clear_cache()


def write_cfg(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "fafo.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_default_is_activity_and_valid_values_parse(tmp_path):
    assert load_config(None).sources["claude"].options == {}
    for mode in ("off", "activity", "prompt"):
        cfg = load_config(write_cfg(tmp_path, f'[sources.claude]\ntask_labels = "{mode}"\n'))
        assert cfg.sources["claude"].options == {"task_labels": mode}


@pytest.mark.parametrize("value", ['"loud"', '""', "5", "true", '["off"]', '"Activity"'])
def test_bad_task_labels_is_config_error_naming_option(tmp_path, value):
    with pytest.raises(ConfigError) as ei:
        load_config(write_cfg(tmp_path, f"[sources.claude]\ntask_labels = {value}\n"))
    assert "sources.claude.task_labels" in str(ei.value)


def test_task_labels_not_allowed_on_other_sources(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write_cfg(tmp_path, '[sources.codex]\ntask_labels = "off"\n'))


def test_registry_passes_mode_to_claude_source(tmp_path):
    cfg = load_config(write_cfg(tmp_path, '[sources.claude]\ntask_labels = "prompt"\n'))
    built = {s.name: s for s in build_sources(cfg, {})}
    assert built["claude"].task_labels == "prompt"
    built = {s.name: s for s in build_sources(load_config(None), {})}
    assert built["claude"].task_labels == "activity"


# -- wiring ---------------------------------------------------------------------------------
def add_transcript(tmp_path: Path, sid: str, lines: list[str], mtime: float = NOW - 5) -> None:
    d = tmp_path / "projects" / "-example-proj"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(f, (mtime, mtime))


def tool_line(name: str, **inp) -> str:
    block = {"type": "tool_use", "id": "tu_x", "name": name, "input": inp}
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [block]}})


def test_label_replaces_state_text_but_not_waiting_text(tmp_path):
    d = make_dir(tmp_path)
    add_transcript(tmp_path, SID_BUSY, [tool_line("Bash", command="make all --secret=x")])
    add_transcript(tmp_path, SID_WAIT, [tool_line("Edit", file_path="/a/b.py")])
    got = {s.pid: s for s in source(d, LIVE).collect(ctx()).sessions}
    assert got[4000].task == "Bash: make"
    assert got[4002].task == "approval for a tool call"  # waiting keeps its waitingFor text
    assert got[4001].task == "idle"  # no transcript: the state, as before


def test_off_and_no_transcript_fall_back_to_state(tmp_path):
    d = make_dir(tmp_path)
    add_transcript(tmp_path, SID_BUSY, [tool_line("Bash", command="make")])
    src = source(d, LIVE)
    src.task_labels = "off"
    assert {s.pid: s.task for s in src.collect(ctx()).sessions}[4000] == "busy"
    assert {s.pid: s.task for s in source(d, LIVE).collect(ctx()).sessions}[4000] == "Bash: make"


def test_label_never_in_logs(tmp_path, caplog):
    d = make_dir(tmp_path)
    add_transcript(tmp_path, SID_BUSY, [tool_line("Edit", file_path="/a/unique-secret-name.py")])
    caplog.set_level(logging.DEBUG)
    res = source(d, LIVE).collect(ctx())
    assert {s.pid: s.task for s in res.sessions}[4000] == "Edit: unique-secret-name.py"
    assert "unique-secret-name" not in caplog.text
