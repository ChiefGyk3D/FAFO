"""Transcript label tests: hand-built made-up fixtures only, injected clock, no real files."""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

import pytest

from fafo import transcript
from fafo.transcript import task_label

FIX = Path(__file__).parent / "fixtures" / "transcripts"
SID = "11111111-aaaa-bbbb-cccc-222222222222"
T0 = 1_700_000_000.0
FAKE_TOKEN = "ghp_exampleexampleexample1234"


@pytest.fixture(autouse=True)
def _fresh_cache():
    transcript.clear_cache()
    yield
    transcript.clear_cache()


def tool(name: str, **inp) -> str:
    block = {"type": "tool_use", "id": "tu_x", "name": name, "input": inp}
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [block]}})


def human(text) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}})


def put(proj: Path, lines, *, sid: str = SID, mtime: float = T0) -> Path:
    d = proj / "-example-proj"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    data = "\n".join(lines) + "\n" if not isinstance(lines, str) else lines
    f.write_text(data, encoding="utf-8")
    os.utime(f, (mtime, mtime))
    return f


def label(proj: Path, mode: str = "activity", now: float = T0 + 5, sid: str = SID) -> str:
    return task_label(sid, proj, mode, now)


def from_fixture(proj: Path, name: str, mtime: float = T0) -> None:
    d = proj / "-example-proj"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{SID}.jsonl"
    shutil.copyfile(FIX / name, f)
    os.utime(f, (mtime, mtime))


# -- activity -----------------------------------------------------------------------------
def test_bash_shows_first_word_only(tmp_path):
    from_fixture(tmp_path, "activity_bash.jsonl")
    got = label(tmp_path)
    assert got == "Bash: deploy-example"
    assert "ghp_" not in got and "--token" not in got


def test_bash_first_word_is_basename_and_capped(tmp_path):
    put(tmp_path, [tool("Bash", command="/usr/local/bin/" + "a" * 40 + " x")])
    assert label(tmp_path) == "Bash"  # over 20 chars: the tool name alone
    put(tmp_path, [tool("Bash", command="/usr/local/bin/make all")])
    transcript.clear_cache()
    assert label(tmp_path) == "Bash: make"


def test_bash_env_assignment_or_token_first_word_never_shown(tmp_path):
    put(tmp_path, [tool("Bash", command=f"SECRET={FAKE_TOKEN} ls")])
    assert label(tmp_path) == "Bash"
    put(tmp_path, [tool("Bash", command=f"{FAKE_TOKEN} ls")])
    transcript.clear_cache()
    assert FAKE_TOKEN not in label(tmp_path)


def test_edit_shows_basename_never_directory(tmp_path):
    from_fixture(tmp_path, "activity_edit.jsonl")
    got = label(tmp_path)
    assert got == "Edit: widget.py"
    assert "private-dir" not in got and "/" not in got


@pytest.mark.parametrize("name", ["Read", "Write", "NotebookEdit"])
def test_file_tools_show_basename(tmp_path, name):
    key = "notebook_path" if name == "NotebookEdit" else "file_path"
    put(tmp_path, [tool(name, **{key: "/a/b/note.ipynb"})])
    assert label(tmp_path) == f"{name}: note.ipynb"


def test_path_with_control_characters(tmp_path):
    esc, bel = chr(27), chr(7)
    put(tmp_path, [tool("Edit", file_path=f"/a/{esc}[31mred{bel}.py")])
    got = label(tmp_path)
    assert esc not in got and bel not in got
    assert got.startswith("Edit: ")


@pytest.mark.parametrize("name", ["Grep", "Glob"])
def test_grep_glob_tool_name_alone(tmp_path, name):
    put(tmp_path, [tool(name, pattern=f"find {FAKE_TOKEN}", path="/secret/dir")])
    assert label(tmp_path) == name


def test_task_agent_subagent_type(tmp_path):
    put(tmp_path, [tool("Task", subagent_type="code-reviewer", prompt="do the secret thing")])
    assert label(tmp_path) == "Task: code-reviewer"
    put(tmp_path, [tool("Agent", subagent_type="bad type; rm", prompt="x")])
    transcript.clear_cache()
    assert label(tmp_path) == "Agent"
    put(tmp_path, [tool("Agent", subagent_type="x" * 31)])
    transcript.clear_cache()
    assert label(tmp_path) == "Agent"


def test_age_suffix_just_under_and_over_120_seconds(tmp_path):
    put(tmp_path, [tool("Bash", command="make")], mtime=T0)
    assert label(tmp_path, now=T0 + 119.9) == "Bash: make"
    assert label(tmp_path, now=T0 + 120) == "Bash: make"
    assert label(tmp_path, now=T0 + 121) == "Bash: make (2m ago)"


def test_age_suffix_minutes_and_cap(tmp_path):
    put(tmp_path, [tool("Edit", file_path="/a/w.py")], mtime=T0)
    assert label(tmp_path, now=T0 + 59 * 60 + 30) == "Edit: w.py (59m ago)"
    assert label(tmp_path, now=T0 + 99 * 60 + 59) == "Edit: w.py (99m ago)"
    assert label(tmp_path, now=T0 + 100 * 60) == "Edit: w.py (>99m ago)"
    assert label(tmp_path, now=T0 + 10**7) == "Edit: w.py (>99m ago)"


def test_age_suffix_survives_the_80_character_cap(tmp_path):
    long_name = "nnnnnnnnn." * 12 + "py"  # dots keep it from looking like a credential run
    put(tmp_path, [tool("Edit", file_path="/a/" + long_name)], mtime=T0)
    got = label(tmp_path, now=T0 + 3600)
    assert len(got) == 80
    assert got.endswith(" (60m ago)")
    assert got.startswith("Edit: nnn")
    assert "idle" not in got


def test_cached_label_gets_a_later_suffix(tmp_path):
    put(tmp_path, [tool("Bash", command="make")], mtime=T0)
    assert label(tmp_path, now=T0 + 10) == "Bash: make"
    assert label(tmp_path, now=T0 + 600) == "Bash: make (10m ago)"


def test_prompt_mode_has_no_age_suffix(tmp_path):
    put(tmp_path, [human("hello there")], mtime=T0)
    assert label(tmp_path, "prompt", now=T0 + 9000) == "hello there"


def test_no_tool_use_returns_empty(tmp_path):
    put(tmp_path, [human("hello there")])
    assert label(tmp_path) == ""
    assert label(tmp_path, now=T0 + 99999) == ""


def test_most_recent_tool_use_wins(tmp_path):
    put(tmp_path, [tool("Read", file_path="/a/old.txt"), tool("Grep", pattern="x")])
    assert label(tmp_path) == "Grep"


# -- prompt -------------------------------------------------------------------------------
def test_prompt_takes_last_human_message_not_tool_result(tmp_path):
    from_fixture(tmp_path, "prompt_mixed.jsonl")
    got = label(tmp_path, "prompt")
    assert got.startswith("second human message")
    assert "tool output" not in got
    assert "first human" not in got


def test_prompt_redacts_token_and_cuts_with_ellipsis(tmp_path):
    from_fixture(tmp_path, "prompt_mixed.jsonl")
    got = label(tmp_path, "prompt")
    assert FAKE_TOKEN not in got and "ghp_" not in got
    assert "<redacted>" in got
    assert got.endswith("...")
    assert len(got) == 63  # 60 characters plus the three-dot ellipsis


def test_prompt_short_message_has_no_ellipsis(tmp_path):
    put(tmp_path, [human("  short\n\n question   here ")])
    assert label(tmp_path, "prompt") == "short question here"


def test_prompt_ignores_tool_result_only_entries(tmp_path):
    result = [{"type": "tool_result", "tool_use_id": "t", "content": "tool says hi"}]
    put(tmp_path, [human("real human words"), human(result)])
    assert label(tmp_path, "prompt") == "real human words"


# -- off and bad modes --------------------------------------------------------------------
def test_off_returns_empty(tmp_path):
    put(tmp_path, [tool("Bash", command="make")])
    assert label(tmp_path, "off") == ""
    assert label(tmp_path, "bogus") == ""


# -- tail-only read -----------------------------------------------------------------------
def test_large_file_reads_only_the_tail(tmp_path, monkeypatch):
    filler = json.dumps({"type": "user", "message": {"role": "user", "content": "x" * 900}})
    lines = [filler] * 400 + [tool("Edit", file_path="/a/tail.py")]
    f = put(tmp_path, lines)
    assert f.stat().st_size > 300_000
    reads: list[int] = []
    real_read = os.read

    def spy(fd, n):
        out = real_read(fd, n)
        reads.append(len(out))
        return out

    monkeypatch.setattr(transcript.os, "read", spy)
    assert label(tmp_path) == "Edit: tail.py"
    assert 0 < sum(reads) <= 65536


def test_half_written_last_line_is_skipped(tmp_path):
    data = tool("Read", file_path="/a/ok.txt") + "\n" + tool("Edit", file_path="/a/half.p")[:-9]
    put(tmp_path, data)
    assert label(tmp_path) == "Read: ok.txt"


def test_invalid_json_lines_skipped(tmp_path):
    put(tmp_path, [tool("Read", file_path="/a/ok.txt"), "not json {", "[1,2,3]", '"str"', "null"])
    assert label(tmp_path) == "Read: ok.txt"


def test_deeply_nested_line_skipped(tmp_path):
    deep = '{"type":"assistant","x":' + "[" * 500 + "]" * 500 + "}"
    put(tmp_path, [tool("Read", file_path="/a/ok.txt"), deep])
    assert label(tmp_path) == "Read: ok.txt"


def test_one_megabyte_line_is_skipped(tmp_path):
    huge = json.dumps({"type": "user", "message": {"role": "user", "content": "y" * 1_000_000}})
    put(tmp_path, [tool("Read", file_path="/a/ok.txt"), huge, tool("Grep", pattern="q")])
    assert label(tmp_path) == "Grep"
    transcript.clear_cache()
    put(tmp_path, [tool("Read", file_path="/a/ok.txt"), huge])
    assert label(tmp_path, "prompt") == ""  # the tail sits inside the huge line


# -- file location ------------------------------------------------------------------------
def test_symlinked_transcript_refused(tmp_path):
    real = tmp_path / "elsewhere.jsonl"
    real.write_text(tool("Bash", command="make") + "\n", encoding="utf-8")
    os.utime(real, (T0, T0))
    d = tmp_path / "proj" / "-example-proj"
    d.mkdir(parents=True)
    (d / f"{SID}.jsonl").symlink_to(real)
    assert label(tmp_path / "proj") == ""


def test_directory_named_like_transcript_refused(tmp_path):
    (tmp_path / "-example-proj" / f"{SID}.jsonl").mkdir(parents=True)
    assert label(tmp_path) == ""


def test_symlinked_project_dir_skipped(tmp_path):
    real = tmp_path / "real" / "-example-proj"
    real.mkdir(parents=True)
    (real / f"{SID}.jsonl").write_text(tool("Bash", command="make") + "\n", encoding="utf-8")
    os.utime(real / f"{SID}.jsonl", (T0, T0))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "linked").symlink_to(real, target_is_directory=True)
    assert label(proj) == ""


def test_fifo_transcript_refused(tmp_path):
    d = tmp_path / "-example-proj"
    d.mkdir()
    os.mkfifo(d / f"{SID}.jsonl")
    assert label(tmp_path) == ""


@pytest.mark.parametrize(
    "sid", ["", "../x", "a/b", "a b", "x" * 129, "id.with.dots", "id\n", "café"]
)
def test_invalid_session_id_returns_empty(tmp_path, sid):
    put(tmp_path, [tool("Bash", command="make")])
    assert label(tmp_path, sid=sid) == ""


def test_missing_projects_dir_and_file(tmp_path):
    assert label(tmp_path / "nope") == ""
    (tmp_path / "-example-proj").mkdir()
    assert label(tmp_path) == ""


# -- output shape -------------------------------------------------------------------------
def test_label_bounded_and_clean(tmp_path):
    esc = chr(27)
    for mode, line in (
        ("activity", tool("Task", subagent_type="a" * 30)),
        ("prompt", human(esc + "[31m" + "word " * 100 + chr(0) + chr(7))),
    ):
        transcript.clear_cache()
        put(tmp_path, [line])
        got = label(tmp_path, mode)
        assert 0 < len(got) <= 80
        assert not any(ord(c) < 32 or ord(c) == 127 for c in got)


def test_never_raises(tmp_path, monkeypatch):
    put(tmp_path, [tool("Bash", command="make")])

    def boom(*a, **k):
        raise OSError("boom")

    monkeypatch.setattr(transcript.os, "open", boom)
    assert label(tmp_path) == ""


# -- cache --------------------------------------------------------------------------------
def test_cache_avoids_rereading_unchanged_file(tmp_path, monkeypatch):
    f = put(tmp_path, [tool("Bash", command="make")])
    opens: list[str] = []
    real_open = os.open

    def spy(path, *a, **k):
        opens.append(os.fspath(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr(transcript.os, "open", spy)
    assert label(tmp_path) == "Bash: make"
    first = sum(p.endswith(".jsonl") for p in opens)
    assert first == 1
    assert label(tmp_path) == "Bash: make"
    assert sum(p.endswith(".jsonl") for p in opens) == first
    # A changed file is read again.
    f.write_text(tool("Grep", pattern="x") + "\n", encoding="utf-8")
    os.utime(f, (T0 + 1, T0 + 1))
    assert label(tmp_path, now=T0 + 3) == "Grep"
    assert sum(p.endswith(".jsonl") for p in opens) == first + 1


def test_cache_is_bounded(tmp_path):
    put(tmp_path, [tool("Bash", command="make")])
    for i in range(transcript.CACHE_MAX + 50):
        put(tmp_path, [tool("Bash", command="make")], sid=f"sid-{i}")
        label(tmp_path, sid=f"sid-{i}")
    assert len(transcript._cache) <= transcript.CACHE_MAX


def test_label_never_logged(tmp_path, caplog):
    put(tmp_path, [tool("Edit", file_path="/a/very-unique-name.py"), "garbage {"])
    caplog.set_level(logging.DEBUG)
    assert label(tmp_path) == "Edit: very-unique-name.py"
    assert label(tmp_path, "prompt") == ""
    assert "very-unique-name" not in caplog.text
    assert str(tmp_path) not in caplog.text
