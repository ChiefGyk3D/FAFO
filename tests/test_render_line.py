"""`fafo line`: one fixed-width status-line summary read from the snapshot."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from fafo import cli
from fafo.model import (
    SNAPSHOT_VERSION,
    Inflight,
    LlmServer,
    LoadedModel,
    Proc,
    Runner,
    Session,
    Snapshot,
    SourceHealth,
    Stamped,
)
from fafo.render import commands
from fafo.render.line import ACTIVE_CPU, ELLIPSIS, NO_COLLECTOR_MARK, render_line
from fafo.store import MAX_SNAPSHOT_BYTES, write_snapshot

NOW = 1_000_000.0
ESC = chr(27)
DIM, RED, RESET = f"{ESC}[2m", f"{ESC}[31m", f"{ESC}[0m"
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "line_busy_80.txt"


def sess(pid, agent="claude", state="busy", name="n", task="t"):
    return Session(
        pid=pid, agent=agent, session_id=f"sid-{pid}", project=Stamped("proj", NOW),
        project_rule="cwd", task=task, state=state, branch="main", worktree="/w",
        started=NOW - 60, cpu=1.0, rss=1024, name=name, address=f"sid-{pid}",
    )


def proc(pid, agent, cpu):
    return Proc(pid=pid, ppid=1, session_pid=None, agent=agent, cmd=agent, cpu=cpu, rss=1, started=NOW)


def runner(i, state):
    return Runner(name=f"r{i}", state=state, repo="", workflow="", job="", step="", step_n=0,
                  step_total=0, elapsed=0.0, load=0.0, mem=0.0, disk=0.0)


def health(**status):
    return [SourceHealth(n, s, NOW - 1, "") for n, s in status.items()]


def snap(*, sessions=(), procs=(), llms=(), runners=(), hl=None, taken=NOW - 2):
    return Snapshot(
        version=SNAPSHOT_VERSION, taken=taken, host={}, sessions=list(sessions),
        procs=list(procs), llms=list(llms), runners=list(runners),
        health=hl if hl is not None else health(claude="ok", codex="ok", ollama="ok",
                                                gemini="ok", runners="ok", copilot="ok"),
    )


def llm(models=1, busy=0, reachable=True):
    return LlmServer(
        models=[LoadedModel(f"m{i}", 1, 1, NOW + 60) for i in range(models)], queue=busy,
        inflight=[Inflight("m0", None, NOW) for _ in range(busy)], gpu={}, reachable=reachable,
    )


def busy_mix():
    return snap(
        sessions=[sess(1), sess(2), sess(3, state="idle"), sess(0, agent="copilot")],
        procs=[proc(10, "codex", 20.0), proc(11, "codex", 0.0), proc(12, "gemini", 9.0)],
        llms=[llm(models=2, busy=1)],
        runners=[runner(1, "busy"), runner(2, "idle"), runner(3, "idle")],
    )


def line(s, **kw):
    kw.setdefault("width", 80)
    kw.setdefault("color", False)
    kw.setdefault("stale_after", 30.0)
    kw.setdefault("quiet", False)
    return render_line(s, NOW, **kw)


# ---- content -------------------------------------------------------------------------------


def test_idle_is_empty():
    s = snap(sessions=[sess(1, state="idle")], procs=[proc(1, "codex", 0.0)], llms=[llm()],
             runners=[runner(1, "idle")])
    assert line(s) == ""


def test_idle_empty_in_cli(tmp_path, capfd):
    p = tmp_path / "s.json"
    write_snapshot(p, snap())
    code = cli.main(["line", "--snapshot", str(p)])
    assert (code, capfd.readouterr().out) == (0, "\n")


def test_busy_mix_segments_in_order():
    # claude 2 of 3 busy, copilot is not a claude session; codex 1 of 2 active; ollama 1 busy of
    # 2 models; gemini 1 call; runners 1 of 3 busy; copilot 1 running
    assert line(busy_mix()) == "cl 2/3 cx 1/2 ol 1/2 gm 1 rn 1/3 cp 1"


def test_golden():
    assert line(busy_mix()) + "\n" == GOLDEN.read_text(encoding="utf-8")


def test_active_cpu_threshold_is_inclusive():
    s = snap(procs=[proc(1, "codex", ACTIVE_CPU), proc(2, "codex", ACTIVE_CPU - 0.01)])
    assert line(s) == "cx 1/2"


def test_ascii_only():
    out = line(busy_mix())
    assert out.isascii() and out.isprintable()


# ---- width ---------------------------------------------------------------------------------


@pytest.mark.parametrize("width", [10, 20, 40, 80])
def test_width_never_exceeded_and_cut_at_segments(width):
    full = line(busy_mix(), width=1000)
    out = line(busy_mix(), width=width)
    assert len(out) <= width
    if len(full) <= width:
        assert out == full
    else:
        assert out.endswith(ELLIPSIS)
        # what is kept is a prefix of whole segments, never a cut token
        segs = ["cl 2/3", "cx 1/2", "ol 1/2", "gm 1", "rn 1/3", "cp 1"]
        text = out[: -len(ELLIPSIS)].rstrip()
        assert any(" ".join(segs[:i]) == text for i in range(len(segs) + 1))


def test_width_expected_values():
    assert line(busy_mix(), width=10) == "cl 2/3 ..."
    assert line(busy_mix(), width=20) == "cl 2/3 cx 1/2 ..."
    assert line(busy_mix(), width=30) == "cl 2/3 cx 1/2 ol 1/2 gm 1 ..."
    assert line(busy_mix(), width=40) == "cl 2/3 cx 1/2 ol 1/2 gm 1 rn 1/3 cp 1"
    assert line(busy_mix(), width=80) == "cl 2/3 cx 1/2 ol 1/2 gm 1 rn 1/3 cp 1"


def test_width_tiny_never_exceeds():
    for w in range(1, 12):
        assert len(line(busy_mix(), width=w)) <= w


def test_width_option_clamped_not_rejected(tmp_path, capfd):
    p = tmp_path / "s.json"
    write_snapshot(p, busy_mix())
    for bad in ("0", "-5", "abc", "99999"):
        assert cli.main(["line", "--snapshot", str(p), "--width", bad]) == 0
        assert len(capfd.readouterr().out.rstrip("\n")) <= commands.LINE_MAX_WIDTH


# ---- color ---------------------------------------------------------------------------------


def stale_down_snap():
    return snap(
        sessions=[sess(1)], procs=[proc(10, "codex", 20.0)], llms=[llm(busy=1)],
        hl=health(claude="stale", codex="down", ollama="ok", gemini="ok", runners="ok",
                  copilot="ok"),
    )


def test_color_off_by_default_even_when_stale_or_down():
    out = line(stale_down_snap())
    assert ESC not in out
    assert out == "cl 1/1 cx 1/1! ol 1/1"


def test_color_dim_stale_red_down():
    out = line(stale_down_snap(), color=True)
    assert out == f"{DIM}cl 1/1{RESET} {RED}cx 1/1!{RESET} ol 1/1"


def test_color_width_counts_visible_chars_only():
    out = line(stale_down_snap(), color=True, width=10)
    visible = out.replace(DIM, "").replace(RED, "").replace(RESET, "")
    assert len(visible) <= 10


def test_down_idle_source_still_shows_marker():
    s = snap(hl=health(ollama="down", claude="ok"), llms=[llm(reachable=False)])
    assert line(s) == "ol!"


def test_stale_idle_source_shows_nothing():
    assert line(snap(hl=health(claude="stale"))) == ""


# ---- missing, old, corrupt, oversized ------------------------------------------------------


def test_old_snapshot_marker():
    assert line(snap(taken=NOW - 31, sessions=[sess(1)])) == NO_COLLECTOR_MARK
    assert line(snap(taken=NOW - 29, sessions=[sess(1)])) != NO_COLLECTOR_MARK


def test_future_snapshot_marker():
    assert line(snap(taken=NOW + 3600, sessions=[sess(1)])) == NO_COLLECTOR_MARK


def test_none_and_quiet():
    assert line(None) == NO_COLLECTOR_MARK
    assert line(None, quiet=True) == ""
    assert line(snap(taken=NOW - 99), quiet=True) == ""


def test_marker_never_colored_and_fits_width():
    assert line(None, color=True) == NO_COLLECTOR_MARK
    assert len(line(None, width=5)) <= 5


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(commands, "_now", lambda: NOW)


@pytest.mark.parametrize("content", [
    None,  # missing
    b"{not json",
    b"",
    b"\x00" * 100,
    b"[" * 100000,
    b"x" * (MAX_SNAPSHOT_BYTES + 1),
])
def test_bad_file_prints_marker_exit0(tmp_path, capfd, content):
    p = tmp_path / "s.json"
    if content is not None:
        p.write_bytes(content)
    code = cli.main(["line", "--snapshot", str(p)])
    cap = capfd.readouterr()
    assert (code, cap.out, "Traceback" in cap.err) == (0, NO_COLLECTOR_MARK + "\n", False)
    code = cli.main(["line", "--snapshot", str(p), "--quiet"])
    assert (code, capfd.readouterr().out) == (0, "\n")


def test_directory_as_snapshot_prints_marker(tmp_path, capfd):
    assert cli.main(["line", "--snapshot", str(tmp_path)]) == 0
    assert capfd.readouterr().out == NO_COLLECTOR_MARK + "\n"


def test_bad_config_still_exit0(tmp_path, capfd):
    cfg = tmp_path / "c.toml"
    cfg.write_text("this is = = not toml", encoding="utf-8")
    assert cli.main(["line", "--config", str(cfg)]) == 0
    out = capfd.readouterr().out
    assert out == NO_COLLECTOR_MARK + "\n"


def test_internal_error_exits0_without_traceback(tmp_path, capfd, monkeypatch):
    from fafo.render import line as linemod

    def boom(*a, **k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(linemod, "render_line", boom)
    p = tmp_path / "s.json"
    write_snapshot(p, busy_mix())
    code = cli.main(["line", "--snapshot", str(p)])
    cap = capfd.readouterr()
    assert code == 0 and cap.out == NO_COLLECTOR_MARK + "\n"
    assert "secret detail" not in cap.out + cap.err and "Traceback" not in cap.err


def test_stale_after_option(tmp_path, capfd):
    p = tmp_path / "s.json"
    write_snapshot(p, snap(taken=NOW - 100, sessions=[sess(1)]))
    cli.main(["line", "--snapshot", str(p)])
    assert capfd.readouterr().out == NO_COLLECTOR_MARK + "\n"
    cli.main(["line", "--snapshot", str(p), "--stale-after", "200"])
    assert capfd.readouterr().out == "cl 1/1\n"


# ---- hostile text --------------------------------------------------------------------------


def test_hostile_strings_never_reach_output(tmp_path, capfd):
    evil = f"{ESC}[31mEVIL{ESC}]0;title\x07\x00\r\nnext"
    s = snap(
        sessions=[sess(1, name=evil, task=evil)],
        procs=[Proc(10, 1, None, evil, evil, 20.0, 1, NOW)],
        runners=[Runner(evil, "busy", evil, evil, evil, evil, 0, 0, 0.0, 0.0, 0.0, 0.0)],
        hl=[SourceHealth(evil, "ok", NOW, evil), SourceHealth("claude", evil, NOW, evil)],
    )
    p = tmp_path / "s.json"
    write_snapshot(p, s)
    cli.main(["line", "--snapshot", str(p), "--color"])
    out = capfd.readouterr().out
    assert "EVIL" not in out and "\x07" not in out and "\x00" not in out
    body = out[:-1]
    assert "\n" not in body and "\r" not in body
    assert body.replace(DIM, "").replace(RED, "").replace(RESET, "").isprintable()


def test_output_is_single_line(tmp_path, capfd):
    p = tmp_path / "s.json"
    write_snapshot(p, busy_mix())
    cli.main(["line", "--snapshot", str(p)])
    out = capfd.readouterr().out
    assert out.endswith("\n") and out.count("\n") == 1


# ---- performance ---------------------------------------------------------------------------


def test_1000_calls_on_500_sessions_quick():
    s = snap(sessions=[sess(i + 1, state="busy" if i % 2 else "idle") for i in range(500)],
             procs=[proc(i + 1000, "codex", float(i % 3)) for i in range(500)])
    start = time.monotonic()
    for _ in range(1000):
        out = line(s)
    elapsed = time.monotonic() - start
    assert out.startswith("cl 250/500")
    assert elapsed < 5.0, elapsed


def test_help_lists_line(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "line" in capsys.readouterr().out
