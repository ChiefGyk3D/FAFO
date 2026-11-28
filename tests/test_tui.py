"""Textual TUI: driven with the pilot against fixture snapshots in tmp_path. No network."""

from __future__ import annotations

import asyncio
import inspect
import time
from pathlib import Path

import pytest

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
from fafo.render import tui
from fafo.render.tui import FafoApp, ReadResult
from fafo.render.view import build_views
from fafo.store import write_snapshot

SID_A = "11111111-aaaa-4aaa-8aaa-000000000001"
SID_B = "22222222-bbbb-4bbb-8bbb-000000000002"
SID_C = "33333333-cccc-4ccc-8ccc-000000000003"
ESC = chr(27)
BEL = chr(7)


def sess(pid, sid, name, project, state="busy", task="work", agent="claude", as_of=None):
    now = time.time()
    return Session(
        pid=pid, agent=agent, session_id=sid, project=Stamped(project, as_of or now),
        project_rule="children-git", task=task, state=state, branch="main",
        worktree="/work/example", started=now - 4320, cpu=3.0, rss=512 * 1024 * 1024,
        name=name, address=sid,
    )


def proc(pid, session_pid, agent="claude", cmd="git status"):
    return Proc(
        pid=pid, ppid=1, session_pid=session_pid, agent=agent, cmd=cmd, cpu=1.0,
        rss=10 * 1024 * 1024, started=time.time() - 60,
    )


def snap(sessions=(), procs=(), taken=None, **kw):
    return Snapshot(
        version=SNAPSHOT_VERSION,
        taken=time.time() if taken is None else taken,
        host={"load1": 4.1, "cpu_percent": 20.0, "mem_percent": 61.0},
        sessions=list(sessions), procs=list(procs), llm=kw.get("llm"),
        runners=kw.get("runners", []), quota=kw.get("quota", {}),
        health=kw.get("health", [SourceHealth("claude", "ok", time.time() - 3, "")]),
    )


def three(extra_procs=()):
    return snap(
        sessions=[
            sess(100, SID_A, "src-1", "alpha"),
            sess(200, SID_B, "src-2", "beta"),
            sess(300, SID_C, "src-3", "gamma"),
        ],
        procs=[
            proc(101, 100, cmd="pytest alpha"),
            proc(102, 100, cmd="git log"),
            proc(201, 200, cmd="ruff check"),
            proc(301, 300, cmd="make beta"),
            proc(500, None, agent="codex", cmd="codex exec run"),
            *extra_procs,
        ],
    )


def many(n):
    return snap(sessions=[sess(1000 + i, f"sid-{i:03d}", f"n{i:03d}", f"p{i:03d}") for i in range(n)])


def run(coro):
    return asyncio.run(coro)


async def settle(pilot, app, reads=1, timeout=8.0):
    """Wait until the app has applied `reads` more refresh results."""
    target = app.reads + reads
    end = time.monotonic() + timeout
    while app.reads < target and time.monotonic() < end:
        await pilot.pause(0.05)
    assert app.reads >= target, "no refresh arrived"
    await pilot.pause(0.05)


async def until(pilot, cond, timeout=30.0):
    """Poll `cond` (short pauses) until it holds; the bound is generous so a loaded host passes."""
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        await pilot.pause(0.05)
    assert cond(), "condition not reached within the bound"
    await pilot.pause(0.05)


async def first(pilot, app, timeout=8.0):
    """Wait for the first applied refresh (it may already have arrived)."""
    end = time.monotonic() + timeout
    while app.reads < 1 and time.monotonic() < end:
        await pilot.pause(0.05)
    assert app.reads >= 1, "no refresh arrived"
    await pilot.pause(0.05)


def make(path, session_id=None, refresh=0.25):
    return FafoApp(path, session_id=session_id, refresh_s=refresh)


@pytest.fixture
def snap_path(tmp_path):
    return tmp_path / "snapshot.json"


def lines_of(app):
    return app.dump().splitlines()


# ---- default view -------------------------------------------------------------------------


def test_default_tree_children_under_sessions_current_first_with_banner(snap_path):
    write_snapshot(snap_path, three())

    async def go():
        app = make(snap_path, SID_C)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            ls = lines_of(app)
            assert ls[0].startswith("you are here:") and "src-3" in ls[0] and "gamma" in ls[0]
            text = "\n".join(ls)
            row = {n: next(i for i, l in enumerate(ls) if n in l and not l.startswith("you"))
                   for n in ("src-1", "src-2", "src-3", "pytest alpha", "git log", "ruff check",
                             "make beta", "codex exec run")}
            assert row["src-3"] < row["make beta"] < row["src-1"]  # current session first
            assert row["src-1"] < row["pytest alpha"] < row["git log"] < row["src-2"]
            assert row["src-2"] < row["ruff check"]
            assert row["codex exec run"] > row["ruff check"]  # unattached group after sessions
            assert "codex" in text
            assert "▶" in ls[row["src-3"]]  # current marker
            assert "view 1/6" in text and "Sessions" in text

    run(go())


def test_unknown_session_banner_exact(snap_path):
    write_snapshot(snap_path, three())

    async def go():
        app = make(snap_path, "nope")
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            assert lines_of(app)[0].strip() == "unknown session"

    run(go())


def test_no_banner_without_session_arg(snap_path):
    write_snapshot(snap_path, three())

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            assert "you are here" not in app.dump()
            assert "unknown session" not in app.dump()

    run(go())


# ---- navigation ---------------------------------------------------------------------------


def test_tab_shift_tab_number_keys_switch_and_wrap(snap_path):
    write_snapshot(snap_path, three())

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            seen = []
            for _ in range(6):
                await pilot.press("tab")
                seen.append(next(l for l in lines_of(app) if "view " in l))
            assert "view 2/6" in seen[0] and "view 6/6" in seen[4] and "view 1/6" in seen[5]
            await pilot.press("shift+tab")
            assert any("view 6/6" in l for l in lines_of(app))
            await pilot.press("3")
            assert any("view 3/6" in l and "Local LLM" in l for l in lines_of(app))
            await pilot.press("2")
            assert any("view 2/6" in l and "Processes" in l for l in lines_of(app))
            await pilot.press("5")
            assert any("view 5/6" in l and "Quota" in l for l in lines_of(app))
            await pilot.press("4")
            assert any("Runners" in l for l in lines_of(app))

    run(go())


def test_j_k_pgdn_pgup_g_G_move_and_indicator_updates(snap_path):
    write_snapshot(snap_path, many(60))

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await first(pilot, app)

            def pos():
                return next(l for l in lines_of(app) if "row " in l and " of " in l)

            assert "row 1 of 60" in pos()
            await pilot.press("j", "j")
            assert "row 3 of 60" in pos()
            await pilot.press("k")
            assert "row 2 of 60" in pos()
            await pilot.press("down", "up", "up")
            assert "row 1 of 60" in pos()
            await pilot.press("pagedown")
            r = int(pos().split("row ")[1].split(" of")[0])
            assert r > 5
            assert f"n{r - 1:03d}" in app.dump()  # selected row is on screen
            await pilot.press("pageup")
            assert "row 1 of 60" in pos()
            await pilot.press("G")
            assert "row 60 of 60" in pos() and "n059" in app.dump()
            await pilot.press("g")
            assert "row 1 of 60" in pos() and "n000" in app.dump()

    run(go())


def test_more_rows_than_fit_never_silently_hidden(snap_path):
    kids = [proc(2000 + i, 100, cmd=f"job{i}") for i in range(260)]
    write_snapshot(snap_path, snap(sessions=[sess(100, SID_A, "src-1", "alpha")], procs=kids))

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            await pilot.press("G")
            assert "+4 more" in app.dump()
            assert "row 258 of 258" in app.dump()  # 1 session + 256 children + the +N more line

    run(go())


def test_filter_narrows_and_esc_clears(snap_path):
    write_snapshot(snap_path, three())

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            await pilot.press("slash")
            await pilot.press(*"BETA")
            await pilot.pause(0.1)
            d = app.dump()
            assert "src-2" in d and "ruff check" in d
            assert "src-1" not in d and "src-3" not in d
            await pilot.press("enter")  # apply, back to the list
            assert "src-1" not in app.dump()
            await pilot.press("escape")
            d = app.dump()
            assert "src-1" in d and "src-2" in d and "src-3" in d

    run(go())


def test_filter_matches_state_agent_project_name(snap_path):
    s = snap(sessions=[
        sess(1, "x1", "aaa", "proj1", state="idle"),
        sess(2, "x2", "bbb", "proj2", state="waiting"),
        sess(3, "x3", "ccc", "proj3", state="busy", agent="codex"),
    ])
    write_snapshot(snap_path, s)

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            for needle, keep in (("waiting", "bbb"), ("PROJ3", "ccc"), ("aaa", "aaa"),
                                 ("codex", "ccc")):
                await pilot.press("slash")
                await pilot.press(*needle)
                await pilot.pause(0.05)
                d = app.dump()
                assert keep in d
                assert sum(n in d for n in ("aaa", "bbb", "ccc")) == 1, needle
                await pilot.press("escape")

    run(go())


def test_enter_drills_in_shows_full_id_esc_returns(snap_path):
    write_snapshot(snap_path, three())

    async def go():
        app = make(snap_path, SID_C)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            await pilot.press("enter")
            d = app.dump()
            assert "view 6/6" in d
            assert SID_C in d  # the full address
            for part in ("src-3", "gamma", "children-git", "/work/example", "main", "300",
                         "make beta"):
                assert part in d, part
            await pilot.press("escape")
            assert "view 1/6" in app.dump()
            assert SID_C not in app.dump()

    run(go())


def test_drill_in_shows_full_redacted_command_wrapped(snap_path):
    long_cmd = "tool " + " ".join(f"<arg{i}>" for i in range(22))
    write_snapshot(snap_path, snap(sessions=[sess(100, SID_A, "src-1", "alpha")],
                                   procs=[proc(101, 100, cmd=long_cmd)]))

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(80, 30)) as pilot:
            await first(pilot, app)
            await pilot.press("enter")
            flat = " ".join(l.strip() for l in lines_of(app))
            assert "<arg21>" in flat and "<arg0>" in flat

    run(go())


def test_collapse_state_and_selection_survive_refresh(snap_path):
    write_snapshot(snap_path, three())

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            await pilot.press("space")  # collapse the first session
            assert "pytest alpha" not in app.dump() and "ruff check" in app.dump()
            await pilot.press("j")  # move to src-2 (children of src-1 are hidden)
            key = app.selected_key
            assert key == ("s", SID_B)
            # a new busy session that sorts BEFORE the others
            s = three()
            s.sessions.insert(0, sess(50, "sid-new", "aaa-new", "aaa", state="busy"))
            s.sessions[1].task = "changed task"
            write_snapshot(snap_path, s)
            await settle(pilot, app, 2)
            assert "aaa-new" in app.dump()
            assert app.selected_key == key  # keyed by session id, not by row index
            assert "pytest alpha" not in app.dump()  # still collapsed
            await pilot.press("right")
            assert "ruff check" in app.dump()

    run(go())


def test_pause_freezes_updates_and_resume_catches_up(snap_path):
    write_snapshot(snap_path, three())

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            await pilot.press("p")
            assert "PAUSED" in app.dump()
            await pilot.pause(0.1)
            s = three()
            s.sessions[0].name = "renamed-while-paused"
            write_snapshot(snap_path, s)
            await pilot.pause(1.0)
            assert "renamed-while-paused" not in app.dump()
            await pilot.press("p")
            assert "PAUSED" not in app.dump()
            await settle(pilot, app, 1)
            assert "renamed-while-paused" in app.dump()

    run(go())


# ---- never blank ---------------------------------------------------------------------------


def test_deleted_snapshot_keeps_last_good_with_stale_tag_never_blank(snap_path):
    write_snapshot(snap_path, three())

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            snap_path.unlink()
            await settle(pilot, app, 2)
            d = app.dump()
            for part in ("src-1", "src-2", "src-3", "pytest alpha"):
                assert part in d, part
            assert "STALE" in d
            assert "Traceback" not in d
            body = [l for l in lines_of(app) if l.strip()]
            assert len(body) > 6

    run(go())


def test_garbage_snapshot_keeps_last_good(snap_path):
    write_snapshot(snap_path, three())

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            snap_path.write_text("{ this is not json " + ESC + "[31m")
            await settle(pilot, app, 2)
            d = app.dump()
            assert "src-1" in d and "STALE" in d
            assert ESC not in d

    run(go())


def test_never_good_shows_collector_not_running(snap_path):
    async def go():
        app = make(snap_path)  # file does not exist
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            assert "collector not running" in app.dump()
            write_snapshot(snap_path, three())
            await settle(pilot, app, 2)
            assert "src-1" in app.dump()
            assert "collector not running" not in app.dump()

    run(go())


def test_exception_in_refresh_path_is_one_footer_line_not_a_traceback(snap_path, monkeypatch):
    write_snapshot(snap_path, three())

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)

            def boom(*a, **k):
                raise RuntimeError("kaput " + ESC + "[31m" + "x" * 500)

            monkeypatch.setattr(FafoApp, "_rebuild", boom)
            s = three()
            s.sessions[0].task = "different"
            write_snapshot(snap_path, s)
            # Wait for the condition itself (the fault line is drawn), not a read count: a read
            # already in flight on the old file counts as a read but never reaches _rebuild.
            await until(pilot, lambda: "kaput" in app.dump())
            ls = lines_of(app)
            status = [l for l in ls if "kaput" in l]
            assert len(status) == 1 and len(status[0]) <= 144
            assert ESC not in app.dump() and "Traceback" not in app.dump()
            assert "src-1" in app.dump()  # screen not blank
            assert app.is_running

    run(go())


# ---- untrusted text -------------------------------------------------------------------------


def test_markup_and_escapes_in_process_name_render_literally(snap_path):
    evil = "[red]boom[/] [bold]x[/bold] [link=http://example.invalid]l[/link]"
    evil_cmd = evil + ESC + "[31mEVIL" + ESC + "]0;pwn" + BEL + "end"
    s = snap(
        sessions=[sess(100, SID_A, "[/]sess[blue]", "alpha")],
        procs=[proc(101, 100, cmd=evil_cmd)],
    )
    write_snapshot(snap_path, s)

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(240, 55)) as pilot:
            await first(pilot, app)
            d = app.dump()
            assert "[red]boom[/] [bold]x[/bold] [link=http://example.invalid]l[/link]" in d
            assert "[/]sess[blue]" in d
            assert ESC not in d and BEL not in d
            assert all(len(l) <= 240 for l in lines_of(app))
            # the row for the evil process is one line and the next session row is intact
            ls = lines_of(app)
            assert sum("[red]boom" in l for l in ls) == 1  # one row, not split across lines
            await pilot.press("enter")
            assert "[red]boom[/]" in app.dump()
            app.export_screenshot()  # the real compositor must not raise either

    run(go())


def test_filter_text_is_not_markup(snap_path):
    write_snapshot(snap_path, three())

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            await pilot.press("slash")
            await pilot.press(*"[red]")
            await pilot.pause(0.05)
            assert "[red]" in app.dump()

    run(go())


# ---- size ---------------------------------------------------------------------------------


def test_too_small_shows_message_and_does_not_crash(snap_path):
    write_snapshot(snap_path, three())

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(40, 10)) as pilot:
            await first(pilot, app)
            assert "terminal too small" in app.dump()
            await pilot.press("tab", "j", "slash", "escape", "enter")
            assert app.is_running

    run(go())


@pytest.mark.parametrize("size", [(80, 24), (144, 40), (240, 55)])
def test_renders_at_widths_without_overflow(snap_path, size):
    w, h = size
    s = three()
    s.llm = LlmServer(
        models=[LoadedModel("gemma-example:12b", 8 * 2**30, 128000, time.time() + 240)],
        queue=1, inflight=[Inflight("gemma-example:12b", 100, time.time() - 5)], gpu={"util": 78.0},
    )
    s.runners = [Runner("r1", "busy", "owner/repo", "ci", "build", "3/7", 3, 7, 130.0, 3.2, 40.0, 50.0)]
    s.quota = {"claude": {"5h": 41, "wk": 63}}
    write_snapshot(snap_path, s)

    async def go():
        app = make(snap_path, SID_A)
        async with app.run_test(size=size) as pilot:
            await first(pilot, app)
            for key in ("1", "2", "3", "4", "5", "6"):
                await pilot.press(key)
                ls = lines_of(app)
                assert max(len(l) for l in ls) <= w, key
                assert len(ls) <= h
            await pilot.press("3")
            d = app.dump()
            assert "gemma-example:12b" in d and "8.0G" in d and "78%" in d
            await pilot.press("4")
            assert "r1" in app.dump() and "owner/repo" in app.dump() or w < 100
            await pilot.press("5")
            assert "claude" in app.dump() and "41" in app.dump()
            app.export_screenshot()

    run(go())


def test_columns_drop_at_narrow_width_but_keep_name_and_state(snap_path):
    write_snapshot(snap_path, three())

    async def go():
        wide = make(snap_path)
        async with wide.run_test(size=(240, 55)) as pilot:
            await first(pilot, wide)
            assert "BRANCH" in wide.dump() and "ELAPSED" in wide.dump()
        narrow = make(snap_path)
        async with narrow.run_test(size=(80, 24)) as pilot:
            await first(pilot, narrow)
            d = narrow.dump()
            assert "BRANCH" not in d and "NAME" in d and "STATE" in d

    run(go())


def test_processes_view_lists_every_process_with_owner(snap_path):
    write_snapshot(snap_path, three())

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            await pilot.press("2")
            ls = lines_of(app)
            for pid in ("101", "102", "201", "301", "500", "100", "200", "300"):
                assert any(l.split() and pid in l.split() for l in ls), pid
            l101 = next(l for l in ls if "pytest alpha" in l)
            assert "src-1" in l101
            l500 = next(l for l in ls if "codex exec run" in l)
            assert "src-" not in l500

    run(go())


# ---- performance ----------------------------------------------------------------------------


def test_large_refresh_ui_thread_time_is_bounded(snap_path):
    now = time.time()
    sessions = [sess(10_000 + i, f"sid-{i:04d}", f"n{i:03d}", f"p{i % 17}") for i in range(200)]
    procs = [proc(1_000_000 + i * 300 + j, 10_000 + i, cmd=f"cmd {j}")
             for i in range(200) for j in range(256)]
    big = snap(sessions=sessions, procs=procs, taken=now)
    views = build_views(big, now)
    write_snapshot(snap_path, three())

    async def go():
        app = make(snap_path, refresh=30)
        async with app.run_test(size=(240, 55)) as pilot:
            await first(pilot, app)
            await pilot.press("p")  # keep the worker out of the measurement
            res = ReadResult(views=views, changed=True, at=time.time(), error="")
            app.handle_result(res)  # warm-up

            def cpu_cost(changed):
                # CPU seconds of the UI thread: invariant to scheduling delay on a loaded host,
                # unlike wall time, yet still grows with real work (a quadratic or blocking
                # refresh burns CPU on this thread).
                t0, w0 = time.thread_time(), time.monotonic()
                app.handle_result(ReadResult(views=views, changed=changed, at=time.time(), error=""))
                return time.thread_time() - t0, time.monotonic() - w0

            samples = [cpu_cost(True) for _ in range(3)]
            changed_cost = min(c for c, _ in samples)  # best CPU sample: noise only adds CPU
            changed_wall = max(w for _, w in samples)  # worst wall sample: a block in any run counts
            await pilot.pause(0.05)
            unchanged_cost, _ = min(cpu_cost(False) for _ in range(3))
            print(f"large refresh cpu: changed {changed_cost * 1000:.1f} ms, "
                  f"unchanged {unchanged_cost * 1000:.1f} ms")
            assert changed_cost < 1.0  # about 0.1 s of CPU unloaded: generous, but bounded
            assert unchanged_cost < 0.1
            # CPU time cannot see a blocked (sleeping) UI thread, but a very loose wall bound
            # can still detect one: it is far above any scheduling delay seen under load.
            assert changed_wall < 10.0
            assert unchanged_cost < changed_cost / 4  # the no-change path skips the rebuild
            assert "row 1 of " in app.dump()

    run(go())


# ---- worker / CLI / theme / surface ------------------------------------------------------------


def test_ui_thread_does_not_read_files(snap_path, monkeypatch):
    """read_views runs only off the UI thread."""
    import threading

    write_snapshot(snap_path, three())
    threads = set()
    real = tui.read_views

    def spy(*a, **k):
        threads.add(threading.current_thread())
        return real(*a, **k)

    monkeypatch.setattr(tui, "read_views", spy)

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            assert threads and threading.main_thread() not in threads

    run(go())


def test_parse_args_validates_refresh_range():
    ok = tui.parse_args(["--snapshot", "x.json", "--refresh", "0.25"])
    assert ok.refresh == 0.25 and ok.session is None
    assert tui.parse_args(["--snapshot", "x.json", "--refresh", "30", "--session", "s"]).session == "s"
    for bad in ("0.24", "30.1", "0", "-1", "nan", "inf", "abc"):
        with pytest.raises(SystemExit):
            tui.parse_args(["--snapshot", "x.json", "--refresh", bad])


def test_entry_point_signatures():
    sig = inspect.signature(tui.run_tui)
    assert list(sig.parameters) == [
        "snapshot_path", "session_id", "refresh_s", "helpers",  # helpers: FAFO#69
    ]
    assert sig.parameters["session_id"].default is None
    assert sig.parameters["refresh_s"].default == 1.0
    assert list(inspect.signature(tui.main).parameters) == ["argv"]


def _lum(hexcolor: str) -> float:
    h = hexcolor.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_dark_theme_only_no_light_fills(snap_path):
    for name, value in tui.PALETTE.items():
        if name.endswith("_bg") or name == "bg":
            assert _lum(value) < 0.2, name
        else:
            assert _lum(value) > 0.25 or name in ("dim",), name  # readable on the dark fill
    write_snapshot(snap_path, three())

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            assert app.current_theme.dark is True
            widgets = [app.screen, *app.screen.walk_children()]
            assert len(widgets) > 4
            for w in widgets:
                base, _ = w.background_colors
                assert base.brightness < 0.3, (w, base)

    run(go())


FORBIDDEN = ("subprocess", "clipboard", "import socket", "httpx", "os.system", "urllib",
             "requests", "pyperclip", "shell=True")


def forbidden_in(source: str) -> list[str]:
    return [f for f in FORBIDDEN if f in source]


def test_no_network_subprocess_or_clipboard_code():
    src = Path(tui.__file__).read_text()
    assert len(src) > 2000  # something was collected
    # the guard bites: it must flag each forbidden construct
    assert forbidden_in("import subprocess") == ["subprocess"]
    assert forbidden_in("app.copy_to_clipboard(x)") == ["clipboard"]
    assert forbidden_in(src) == []
    assert chr(0x2014) not in src  # no em dash


def test_quits_on_q(snap_path):
    write_snapshot(snap_path, three())

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            await pilot.press("q")
            await pilot.pause(0.1)
            assert not app.is_running or app._exit

    run(go())


def test_change_read_while_paused_shows_after_resume(snap_path):
    """A read made while paused (and dropped) must not be recorded as seen."""
    a = three()
    a.sessions[0].name = "name-A"
    a.health = []  # health ages tick every second and would mask the bug
    write_snapshot(snap_path, a)

    async def go():
        app = make(snap_path, refresh=0.25)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            assert "name-A" in app.dump()
            # a read that is already in flight when pause is pressed gets dropped by the UI
            app.paused = True  # UI side drops results; the reader keeps reading
            b = three()
            b.sessions[0].name = "name-B"
            b.health = []
            write_snapshot(snap_path, b)
            await pilot.pause(1.0)  # several reads of B happen and are dropped
            assert "name-B" not in app.dump()
            app.paused = False  # resume
            await pilot.pause(0.9)  # within a few refresh intervals
            assert "name-B" in app.dump()
            assert "name-A" not in app.dump()

    run(go())


def test_unchanged_file_after_resume_does_not_rebuild(snap_path, monkeypatch):
    s = three()
    s.health = []  # health ages tick every second and would count as a change
    write_snapshot(snap_path, s)
    rebuilds = []
    real = FafoApp._rebuild

    def counting(self):
        rebuilds.append(1)
        return real(self)

    monkeypatch.setattr(FafoApp, "_rebuild", counting)

    async def go():
        app = make(snap_path, refresh=0.25)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            await pilot.press("p")
            await pilot.pause(0.6)
            await pilot.press("p")
            n = len(rebuilds)
            await settle(pilot, app, 3)
            assert len(rebuilds) == n  # nothing changed, the cheap path ran
            assert "src-1" in app.dump()

    run(go())


def test_tui_shows_last_known_project_with_tilde(snap_path):
    last = sess(1, "x1", "aaa", "chatty-kathy", state="idle")
    last.project_source = "last"
    write_snapshot(snap_path, snap(sessions=[last]))

    async def go():
        app = make(snap_path)
        async with app.run_test(size=(144, 40)) as pilot:
            await first(pilot, app)
            assert "chatty-kathy~" in app.dump()

    run(go())
