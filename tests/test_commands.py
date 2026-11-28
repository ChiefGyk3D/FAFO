"""Render commands: table, json, sessions, tui, --session auto, and the example systemd unit."""

from __future__ import annotations

import configparser
import itertools
import json
import os
import re
import sys
import types
from pathlib import Path

import pytest

from fafo import cli
from fafo.model import SNAPSHOT_VERSION, Proc, Session, Snapshot, SourceHealth, Stamped
from fafo.render import commands
from fafo.render.table import format_json, format_sessions, format_table
from fafo.render.view import Banner, build_views
from fafo.store import write_snapshot

NOW = 1_000_000.0
ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "examples" / "systemd" / "fafo.service"
SID_A = "11111111-aaaa-4aaa-8aaa-000000000001"
SID_B = "22222222-bbbb-4bbb-8bbb-000000000002"
ESC = chr(27)
EM = chr(0x2014)


def sess(pid, sid, name, project="alpha-proj", agent="claude", task="work"):
    return Session(
        pid=pid, agent=agent, session_id=sid, project=Stamped(project, NOW), project_rule="cwd",
        task=task, state="busy", branch="main", worktree="/work/example", started=NOW - 600,
        cpu=1.0, rss=64 * 1024 * 1024, name=name, address=sid,
    )


def snap_with(*sessions, procs=()):
    return Snapshot(
        version=SNAPSHOT_VERSION, taken=NOW - 2, host={"load1": 1.5},
        sessions=list(sessions), procs=list(procs),
        health=[SourceHealth("claude", "ok", NOW - 2, "")],
    )


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(commands, "_now", lambda: NOW)
    monkeypatch.setattr(commands, "_getpid", lambda: 4000)
    monkeypatch.setattr(commands, "_parent_of", lambda pid: None)
    monkeypatch.delenv("COLUMNS", raising=False)


@pytest.fixture
def snap_path(tmp_path):
    p = tmp_path / "state" / "snapshot.json"
    write_snapshot(p, snap_with(sess(100, SID_A, "src-1"), sess(200, SID_B, "src-2", "beta-proj")))
    return p


def run(capfdbinary, *argv):
    code = cli.main(list(argv))
    cap = capfdbinary.readouterr()
    return code, cap.out.decode("utf-8"), cap.err.decode("utf-8"), cap.out


# ---- output equals the view model ---------------------------------------------------------


def test_table_matches_format_table(snap_path, capfdbinary):
    code, out, _, _ = run(capfdbinary, "table", "--snapshot", str(snap_path), "--width", "90")
    assert code == 0
    expected = format_table(build_views(snap_with(sess(100, SID_A, "src-1"),
                                                  sess(200, SID_B, "src-2", "beta-proj")), NOW), 90)
    assert out == expected
    assert "src-1" in out and "src-2" in out


def test_json_matches_format_json(snap_path, capfdbinary):
    code, out, _, _ = run(capfdbinary, "json", "--snapshot", str(snap_path))
    assert code == 0
    snap = snap_with(sess(100, SID_A, "src-1"), sess(200, SID_B, "src-2", "beta-proj"))
    assert out == format_json(snap, NOW)
    assert json.loads(out)["status"] == "ok"


def test_sessions_matches_format_sessions_and_marks_current(snap_path, capfdbinary):
    code, out, _, _ = run(capfdbinary, "sessions", "--snapshot", str(snap_path), "--session", SID_B)
    assert code == 0
    snap = snap_with(sess(100, SID_A, "src-1"), sess(200, SID_B, "src-2", "beta-proj"))
    assert out == format_sessions(build_views(snap, NOW, current_session_id=SID_B))
    lines = out.splitlines()
    assert len(lines) == 2
    cur = [ln for ln in lines if SID_B in ln]
    other = [ln for ln in lines if SID_A in ln]
    assert len(cur) == 1 and cur[0].startswith("* ")
    assert len(other) == 1 and other[0].startswith("  ")


def test_table_banner_for_explicit_session(snap_path, capfdbinary):
    _, out, _, _ = run(capfdbinary, "table", "--snapshot", str(snap_path), "--session", SID_A)
    assert "you are here: src-1" in out


# ---- missing snapshot ----------------------------------------------------------------------


def test_missing_snapshot_is_not_an_error(tmp_path, capfdbinary):
    missing = str(tmp_path / "nope.json")
    code, out, _, _ = run(capfdbinary, "table", "--snapshot", missing)
    assert code == 0
    assert out == format_table(build_views(None, NOW), commands.default_width())
    assert "collector not running" in out
    code, out, _, _ = run(capfdbinary, "json", "--snapshot", missing)
    assert code == 0 and json.loads(out) == {"status": "no-collector"}
    code, out, err, _ = run(capfdbinary, "sessions", "--snapshot", missing)
    assert code == 0 and out == "" and "collector not running" in err


def test_unreadable_snapshot_is_not_an_error(tmp_path, capfdbinary):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    code, out, _, _ = run(capfdbinary, "table", "--snapshot", str(bad))
    assert code == 0 and "collector not running" in out


def fake_tui(monkeypatch, result=0):
    calls = []
    mod = types.ModuleType("fafo.render.tui")

    def run_tui(path, session_id=None, refresh_s=1.0, helpers=None):
        calls.append((Path(path), session_id, refresh_s))
        return result

    mod.run_tui = run_tui
    monkeypatch.setitem(sys.modules, "fafo.render.tui", mod)
    return calls


def test_tui_passes_arguments_and_exit_code(tmp_path, monkeypatch, capfdbinary):
    calls = fake_tui(monkeypatch, result=0)
    missing = tmp_path / "nope.json"
    code, _, _, _ = run(capfdbinary, "tui", "--snapshot", str(missing), "--session", SID_A,
                        "--refresh", "2")
    assert code == 0
    assert calls == [(missing, SID_A, 2.0)]


def test_tui_missing_textual_prints_hint_and_exits_2(monkeypatch, tmp_path, capfdbinary):
    monkeypatch.setitem(sys.modules, "fafo.render.tui", None)  # import raises ImportError
    code, out, err, _ = run(capfdbinary, "tui", "--snapshot", str(tmp_path / "x.json"))
    assert code == 2
    assert out == ""
    assert len(err.strip().splitlines()) == 1 and "pip install" in err


def test_other_commands_work_without_textual(monkeypatch, snap_path, capfdbinary):
    monkeypatch.setitem(sys.modules, "fafo.render.tui", None)
    for cmd in ("table", "json", "sessions"):
        code, _, _, _ = run(capfdbinary, cmd, "--snapshot", str(snap_path))
        assert code == 0


@pytest.mark.parametrize("bad", ["0", "nan", "abc", "1000"])
def test_tui_bad_refresh_exits_2(bad, monkeypatch, tmp_path, capfdbinary):
    fake_tui(monkeypatch)
    with pytest.raises(SystemExit) as ei:
        cli.main(["tui", "--snapshot", str(tmp_path / "x"), "--refresh", bad])
    assert ei.value.code == 2


# ---- snapshot path and config --------------------------------------------------------------


def test_snapshot_path_precedence(tmp_path, capfdbinary):
    from_cfg = tmp_path / "cfg-snap.json"
    from_flag = tmp_path / "flag-snap.json"
    write_snapshot(from_cfg, snap_with(sess(100, SID_A, "from-config")))
    write_snapshot(from_flag, snap_with(sess(100, SID_A, "from-flag")))
    cfg = tmp_path / "fafo.toml"
    cfg.write_text(f'snapshot_path = "{from_cfg}"\n', encoding="utf-8")
    _, out, _, _ = run(capfdbinary, "sessions", "--config", str(cfg))
    assert "from-config" in out
    _, out, _, _ = run(capfdbinary, "sessions", "--config", str(cfg), "--snapshot", str(from_flag))
    assert "from-flag" in out and "from-config" not in out


def test_bad_config_exits_2_value_free(tmp_path, capfdbinary):
    cfg = tmp_path / "fafo.toml"
    cfg.write_text('snapshot_path = "x"\nbogus_key = "SECRET-VALUE-123"\n', encoding="utf-8")
    for cmd in ("table", "json", "sessions", "tui"):
        code, out, err, _ = run(capfdbinary, cmd, "--config", str(cfg))
        assert code == 2 and out == ""
        assert "SECRET-VALUE-123" not in err and "config error" in err


# ---- session ids and ancestry --------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", ["", "a b", "x" * 129, "../etc", "a/b", "a\nb", ESC + "[2J", "id;rm"]
)
def test_bad_session_id_exits_2_without_echo(bad, tmp_path, capfdbinary):
    with pytest.raises(SystemExit) as ei:
        cli.main(["table", "--snapshot", str(tmp_path / "x"), "--session", bad])
    assert ei.value.code == 2
    err = capfdbinary.readouterr().err
    assert ESC.encode() not in err
    assert b"invalid session id" in err


def test_session_id_pattern_accepts_documented_set():
    for ok in ("a", "A.b_c:d-e", "x" * 128, SID_A):
        assert commands.session_arg(ok) == ok
    assert commands.session_arg("auto") == "auto"


def chain(*pids):
    """parent_of for a straight chain pids[0] -> pids[1] -> ..."""
    nxt = dict(itertools.pairwise(pids))
    return lambda pid: nxt.get(pid)


SESSIONS = [sess(500, SID_A, "near"), sess(900, SID_B, "far")]


def test_nearest_ancestor_wins():
    got = commands.find_current_session(SESSIONS, 4000, chain(4000, 4001, 500, 777, 900))
    assert got == SID_A


def test_nearest_ancestor_wins_listing_order_reversed():
    got = commands.find_current_session(list(reversed(SESSIONS)), 4000,
                                        chain(4000, 4001, 500, 777, 900))
    assert got == SID_A


def test_no_match_gives_none():
    assert commands.find_current_session(SESSIONS, 4000, chain(4000, 4001, 4002)) is None


def test_other_agents_and_pidless_sessions_never_match():
    other = [sess(500, "gem-1", "x", agent="gemini"), sess(0, "run-1", "y", agent="copilot")]
    assert commands.find_current_session(other, 500, chain(500, 0)) is None


def test_vanished_process_is_tolerated():
    def parent_of(pid):
        raise_for = {4001: None}
        return {4000: 4001}.get(pid, raise_for.get(pid))

    assert commands.find_current_session(SESSIONS, 4000, parent_of) is None


def test_exception_from_ancestry_is_tolerated():
    def parent_of(pid):
        raise ProcessLookupError(pid)

    assert commands.find_current_session(SESSIONS, 500, parent_of) == SID_A  # self matches
    assert commands.find_current_session(SESSIONS, 4000, parent_of) is None


def test_cycle_terminates():
    cyc = {4000: 4001, 4001: 4002, 4002: 4000}
    assert commands.find_current_session(SESSIONS, 4000, lambda p: cyc.get(p)) is None


def test_hop_cap_is_64():
    pids = [10_000 + i for i in range(200)]
    near_enough = [sess(pids[63], SID_A, "n")]
    too_far = [sess(pids[70], SID_A, "n")]
    assert commands.find_current_session(near_enough, pids[0], chain(*pids)) == SID_A
    assert commands.find_current_session(too_far, pids[0], chain(*pids)) is None


def test_default_parent_of_real_and_vanished():
    assert commands._real_parent_of(os.getpid()) == os.getppid()
    assert commands._real_parent_of(2**31 - 2) is None


def test_auto_end_to_end(snap_path, monkeypatch, capfdbinary):
    monkeypatch.setattr(commands, "_parent_of", chain(4000, 150, 100, 1))
    _, out, _, _ = run(capfdbinary, "table", "--snapshot", str(snap_path), "--session", "auto")
    assert "you are here: src-1" in out
    _, out, _, _ = run(capfdbinary, "sessions", "--snapshot", str(snap_path), "--session", "auto")
    assert out.splitlines()[0].startswith("* ")


def test_auto_without_match_is_silent(snap_path, capfdbinary):
    code, out, err, _ = run(capfdbinary, "table", "--snapshot", str(snap_path), "--session", "auto")
    assert code == 0 and err == ""
    assert "you are here" not in out and "unknown session" not in out
    assert out == format_table(build_views(
        snap_with(sess(100, SID_A, "src-1"), sess(200, SID_B, "src-2", "beta-proj")), NOW),
        commands.default_width())


def test_auto_passes_resolved_id_to_tui(snap_path, monkeypatch, capfdbinary):
    calls = fake_tui(monkeypatch)
    monkeypatch.setattr(commands, "_parent_of", chain(4000, 200))
    run(capfdbinary, "tui", "--snapshot", str(snap_path), "--session", "auto")
    run(capfdbinary, "tui", "--snapshot", str(snap_path))
    assert [c[1] for c in calls] == [SID_B, None]


# ---- hostile text, width -------------------------------------------------------------------


HOSTILE = f"ev{ESC}[31mil{ESC}]0;title\x07\x08name\x9b2J"


def test_hostile_names_never_reach_stdout_bytes(tmp_path, capfdbinary):
    p = tmp_path / "s.json"
    write_snapshot(p, snap_with(sess(100, SID_A, HOSTILE, project=HOSTILE, task=HOSTILE),
                                procs=[Proc(101, 1, 100, "claude", HOSTILE, 1.0, 1, NOW - 5)]))
    for cmd in ("table", "sessions", "json"):
        extra = [] if cmd == "json" else ["--session", SID_A]
        _, _, _, raw = run(capfdbinary, cmd, "--snapshot", str(p), *extra)
        assert raw
        assert ESC.encode() not in raw and b"\x07" not in raw and b"\x08" not in raw
        assert b"\xc2\x9b" not in raw
        assert not [b for b in raw if b < 0x20 and b != 0x0A]


def test_table_strips_controls_even_if_view_model_regresses(monkeypatch, capfdbinary):
    """Defense in depth: the command re-strips, so a view model bug cannot reach the terminal."""
    views = build_views(snap_with(sess(100, SID_A, "ok")), NOW)
    views.banner = Banner(f"{ESC}[2Jpwned{ESC}]0;t\x07")
    views.sessions[0].name = f"x{ESC}[31mred\x08"
    monkeypatch.setattr(commands, "build_views", lambda *a, **k: views)
    code, _, _, raw = run(capfdbinary, "table", "--snapshot", "/nonexistent/x.json")
    assert code == 0 and b"pwned" in raw
    assert ESC.encode() not in raw and b"\x07" not in raw and b"\x08" not in raw


@pytest.mark.parametrize("width", [20, 40, 60, 100])
def test_no_line_exceeds_width(width, snap_path, capfdbinary):
    _, out, _, _ = run(capfdbinary, "table", "--snapshot", str(snap_path), "--width", str(width))
    assert out.strip()
    assert all(len(line) <= width for line in out.splitlines())


@pytest.mark.parametrize("bad", ["0", "19", "301", "x", "-5"])
def test_bad_width_exits_2(bad, tmp_path):
    with pytest.raises(SystemExit) as ei:
        cli.main(["table", "--snapshot", str(tmp_path / "x"), "--width", bad])
    assert ei.value.code == 2


def test_default_width_follows_terminal_and_clamps(monkeypatch):
    monkeypatch.setenv("COLUMNS", "120")
    assert commands.default_width() == 120
    monkeypatch.setenv("COLUMNS", "10")
    assert commands.default_width() == 40
    monkeypatch.setenv("COLUMNS", "999")
    assert commands.default_width() == 300


def test_commands_listed_in_help(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for name in ("run", "doctor", "table", "json", "sessions", "tui"):
        assert name in out


def test_help_documents_auto_limit(capsys):
    with pytest.raises(SystemExit):
        cli.main(["table", "--help"])
    flat = " ".join(capsys.readouterr().out.split())
    assert "terminal inside a Claude or Codex session" in flat


# ---- example systemd unit ------------------------------------------------------------------


def parse_unit():
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read_string(UNIT.read_text(encoding="utf-8"))
    return cp


def test_unit_file_exists_and_hardened():
    cp = parse_unit()
    svc = cp["Service"]
    assert svc["ExecStart"] == "%h/.local/bin/fafo run"
    assert svc["Restart"] == "on-failure"
    assert svc["RestartSec"]
    assert svc["NoNewPrivileges"] == "yes"
    assert svc["PrivateTmp"] == "yes"
    assert svc["ProtectSystem"] == "strict"
    assert "ReadWritePaths" in svc and "%h" not in svc["ReadWritePaths"].replace("%h/", "")
    assert svc["RestrictAddressFamilies"].split() == ["AF_UNIX", "AF_INET", "AF_INET6"]
    assert svc["LockPersonality"] == "yes"
    assert svc["CapabilityBoundingSet"] == ""
    assert "ProtectHome" not in svc  # would hide ~/.claude/sessions
    assert cp["Install"]["WantedBy"] == "default.target"


def test_unit_no_secrets_no_home_no_em_dash():
    text = UNIT.read_text(encoding="utf-8")
    assert EM not in text
    assert "/home/" not in text and "/root" not in text
    for line in text.splitlines():
        if line.startswith("Environment="):
            assert not re.search(r"(?i)token|secret|key|pass|[A-Za-z0-9]{24,}", line), line
    assert "#Environment=" in text or "# Environment=" in text  # shown only as a comment
    assert "token_file" in text


def test_unit_guard_bites():
    bad = "Environment=GH_TOKEN=ghp_" + "a" * 30
    assert re.search(r"(?i)token|[A-Za-z0-9]{24,}", bad)


def test_systemd_readme_exists_without_em_dash():
    text = (ROOT / "examples" / "systemd" / "README.md").read_text(encoding="utf-8")
    assert EM not in text and "daemon-reload" in text


# ---- strict JSON from the command ----------------------------------------------------------


def test_json_command_prints_strict_json_for_nonfinite_values(snap_path, monkeypatch, capfdbinary):
    snap = snap_with(sess(100, SID_A, "src-1"))
    snap.host["load1"] = float("nan")
    monkeypatch.setattr(commands, "read_snapshot", lambda p: snap)

    def refuse(name):
        raise AssertionError(name)

    code, out, _, _ = run(capfdbinary, "json", "--snapshot", str(snap_path))
    assert code == 0
    assert json.loads(out, parse_constant=refuse)["host"]["load1"] is None


def test_json_command_with_nan_in_file_is_no_collector(tmp_path, capfdbinary):
    p = tmp_path / "s.json"
    write_snapshot(p, snap_with(sess(100, SID_A, "src-1")))
    p.write_text(p.read_text(encoding="utf-8").replace('"load1":1.5', '"load1":NaN'),
                 encoding="utf-8")
    code, out, _, _ = run(capfdbinary, "json", "--snapshot", str(p))
    assert code == 0 and json.loads(out) == {"status": "no-collector"}


# ---- README shell blocks -------------------------------------------------------------------

FENCE = re.compile(r"^```(?:sh|bash|shell|console)?\s*$")
HEADING = re.compile(r"^#{1,6}\s")
MARKER = "<!-- fafo-readme-rule: requires-install-above -->"
README = ROOT / "README.md"


def readme_command_problems(text: str) -> list[str]:
    """README rule for bare `fafo <cmd>` lines (a line that starts with `fafo`) in fenced
    shell blocks (no language, sh, bash, shell or console).

    A bare command passes only if one of these holds:
    - an earlier line in the SAME SECTION (since the last Markdown heading line outside a
      fence, any `#` level) inside a shell block is `pipx install ...` or `source .../activate`
      (the same block counts); a plain `pip install` does not, its binary is not on PATH; or
    - the block's opening fence is on the line immediately after the marker line
      `<!-- fafo-readme-rule: requires-install-above -->` AND a `pipx install` line appeared
      earlier in the README. The marker means "this block follows the pipx install above"; it
      is never valid for the venv path, whose commands use the full path instead.
    Commands invoked by full path never start with `fafo` and always pass. A heading resets the
    section state, so an install line in an earlier section does not carry over.
    """
    problems: list[str] = []
    in_block = False
    ready = False  # install or activate seen in this section
    pipx_seen = False  # a pipx install line appeared anywhere earlier
    marked = False  # the current block was opened right after the marker
    prev = ""
    for n, line in enumerate(text.splitlines(), 1):
        before, prev = prev, line.strip()
        if line.startswith("```"):
            if in_block:
                in_block = False
            elif FENCE.match(line):
                in_block = True
                marked = before == MARKER and pipx_seen
            continue
        if not in_block:
            if HEADING.match(line):
                ready = False
            continue
        stripped = line.strip()
        if "pipx install " in stripped or stripped == "pipx install":
            ready = pipx_seen = True
        elif re.search(r"(?:^|\s)(?:source|\.)\s+\S*activate\b", stripped):
            ready = True
        elif re.match(r"fafo(\s|$)", stripped) and not (ready or marked):
            problems.append(f"README.md:{n}: {stripped}")
    return problems


def test_readme_bare_fafo_commands_follow_an_install_line():
    text = README.read_text(encoding="utf-8")
    assert len(re.findall(r"^fafo ", text, re.MULTILINE)) >= 5  # the quickstart commands were found
    assert "fafo-venv/bin/fafo" in text  # the venv path uses full paths
    assert readme_command_problems(text) == []
    assert EM not in text


def test_readme_rule_bites():
    bad = "```\nfafo doctor\n```\n"
    assert readme_command_problems(bad) == ["README.md:2: fafo doctor"]
    wrong_install = "```\npip install .\nfafo doctor\n```\n"
    assert readme_command_problems(wrong_install)  # a bare pip install is not on PATH
    ok = "```\npipx install .\nfafo doctor\n```\n"
    assert readme_command_problems(ok) == []
    activated = "```\nsource ~/v/bin/activate\nfafo doctor\n```\n"
    assert readme_command_problems(activated) == []
    full = "```\n~/.local/share/fafo-venv/bin/fafo doctor\n```\n"
    assert readme_command_problems(full) == []
    # an install line in prose or in a non-shell block does not count
    assert readme_command_problems("pipx install .\n```\nfafo doctor\n```\n")


def test_readme_rule_is_section_scoped():
    venv_only = ("## Venv\n\n```\npython3 -m venv v\nv/bin/pip install .\n"
                 "v/bin/fafo doctor\nfafo run\n```\n")
    assert len(readme_command_problems(venv_only)) == 1  # only the bare `fafo run`
    prev_section = ("## One\n```\npipx install .\nfafo doctor\n```\n"
                    "## Two\n```\nfafo run\n```\n")
    got = readme_command_problems(prev_section)
    assert len(got) == 1 and got[0].endswith("fafo run")
    # a heading of any level resets; a `#` line inside a fence is a comment, not a heading
    for h in ("# H", "### H", "###### H"):
        assert readme_command_problems(f"pipx x\n```\npipx install .\n```\n{h}\n```\nfafo a\n```\n")
    same = "## One\n```\npipx install .\n```\ntext\n```\nfafo run\n```\n"
    assert readme_command_problems(same) == []
    comment = "## One\n```\npipx install .\n# comment\nfafo run\n```\n"
    assert readme_command_problems(comment) == []


def test_readme_marker_rules():
    install = "## One\n```\npipx install .\n```\n"
    marked = install + "## Two\n" + MARKER + "\n```\nfafo run\n```\n"
    assert readme_command_problems(marked) == []
    # the marker must be on the line immediately before the fence
    gap = install + "## Two\n" + MARKER + "\n\n```\nfafo run\n```\n"
    assert len(readme_command_problems(gap)) == 1
    # it covers only the block that follows it
    two = install + "## Two\n" + MARKER + "\n```\nfafo run\n```\n```\nfafo table\n```\n"
    got = readme_command_problems(two)
    assert len(got) == 1 and got[0].endswith("fafo table")
    # with no pipx install anywhere earlier (venv path only) the marker is not honored
    venv = "## V\n```\nv/bin/pip install .\n```\n## Two\n" + MARKER + "\n```\nfafo run\n```\n"
    assert len(readme_command_problems(venv)) == 1


def test_unit_default_matches_readme_and_both_paths_documented():
    text = (ROOT / "examples" / "systemd" / "README.md").read_text(encoding="utf-8")
    assert "~/.local/bin/fafo" in text and "fafo-venv/bin/fafo" in text


# ---- the helper-expansion cap is documented -----------------------------------------------------


def _helper_text(path: Path) -> str:
    """The prose that describes the helper expansion, with comment markers and wrapping removed."""
    text = path.read_text(encoding="utf-8")
    flat = " ".join(line.lstrip("# ").strip() for line in text.splitlines())
    start = flat.find("lists up to")
    assert start != -1, f"{path.name}: helper expansion text not found"
    return flat[start : start + 200]


def helper_cap_documented(text: str, cap: int) -> bool:
    return f"lists up to {cap} of that session's helpers" in text and "not shown" in text.replace(
        "the ones not shown", "not shown"
    )


def test_helper_cap_and_hidden_count_are_documented():
    from fafo.render.view import MAX_CHILDREN

    for path in (README, ROOT / "config.example.toml"):
        text = _helper_text(path)
        assert helper_cap_documented(text, MAX_CHILDREN), (path.name, text)


def test_helper_cap_check_bites():
    from fafo.render.view import MAX_CHILDREN

    good = _helper_text(README)
    assert helper_cap_documented(good, MAX_CHILDREN)
    assert not helper_cap_documented(good.replace(str(MAX_CHILDREN), str(MAX_CHILDREN + 1)), MAX_CHILDREN)
    assert not helper_cap_documented(good.replace("not shown", "hidden"), MAX_CHILDREN)
