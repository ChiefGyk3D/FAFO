"""TUI: the helper summary row, and the per-session expand toggle."""

from __future__ import annotations

import asyncio
import time

import pytest

from fafo.model import SNAPSHOT_VERSION, Proc, Session, Snapshot, Stamped
from fafo.render import tui
from fafo.render.tui import FafoApp
from fafo.store import write_snapshot

SID_A = "11111111-aaaa-4aaa-8aaa-000000000001"
SID_B = "22222222-bbbb-4bbb-8bbb-000000000002"


def sess(pid, sid, name):
    now = time.time()
    return Session(
        pid=pid, agent="claude", session_id=sid, project=Stamped(name, now),
        project_rule="cwd-name", task="work", state="busy", branch="main",
        worktree="/work/example", started=now - 100, cpu=1.0, rss=1024, name=name, address=sid,
    )


def proc(pid, owner, cmd):
    return Proc(pid, 1, owner, "claude", cmd, 0.5, 2048, time.time() - 5)


def snapshot():
    procs = [proc(1100 + i, 100, "sleep 5") for i in range(3)]
    procs += [proc(1200, 100, "pytest -q"), proc(2100, 200, "sleep 5"), proc(2200, 200, "make")]
    return Snapshot(
        version=SNAPSHOT_VERSION, taken=time.time(), host={},
        sessions=[sess(100, SID_A, "src-a"), sess(200, SID_B, "src-b")], procs=procs,
    )


async def wait_read(pilot, app):
    end = time.monotonic() + 8
    while app.reads < 1 and time.monotonic() < end:
        await pilot.pause(0.05)
    assert app.reads >= 1
    await pilot.pause(0.05)


def lines(app):
    return app.dump().splitlines()


def select(app, kind, key0):
    for i, it in enumerate(app.page.items):
        if it.kind == kind and it.key[1] == key0:
            app.sel_idx = i
            app.sel_key = it.key
            return
    raise AssertionError(f"no {kind} item for {key0}")


def test_summary_row_shown_and_helpers_hidden_by_default(tmp_path):
    path = tmp_path / "snapshot.json"
    write_snapshot(path, snapshot())

    async def go():
        app = FafoApp(path, refresh_s=0.25)
        async with app.run_test(size=(144, 40)) as pilot:
            await wait_read(pilot, app)
            text = "\n".join(lines(app))
            assert "+3 helpers (sleep x3)" in text
            assert "+1 helpers (sleep x1)" in text
            assert "pytest -q" in text and "make" in text
            assert "sleep 5" not in text

    asyncio.run(go())


def test_enter_and_x_toggle_expansion_per_session(tmp_path):
    path = tmp_path / "snapshot.json"
    write_snapshot(path, snapshot())

    async def go():
        app = FafoApp(path, refresh_s=0.25)
        async with app.run_test(size=(144, 40)) as pilot:
            await wait_read(pilot, app)
            select(app, "helpers", SID_A)
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert app.view_idx == tui.V_SESSIONS  # Enter did not open the drill-in
            text = "\n".join(lines(app))
            assert text.count("sleep 5") == 3  # session A only; B still collapsed
            assert "+1 helpers (sleep x1)" in text
            # x toggles it back, from the summary row
            select(app, "helpers", SID_A)
            await pilot.press("x")
            await pilot.pause(0.1)
            assert "sleep 5" not in "\n".join(lines(app))
            # x on the session row toggles that session's helpers
            select(app, "sess", SID_B)
            await pilot.press("x")
            await pilot.pause(0.1)
            assert "\n".join(lines(app)).count("sleep 5") == 1
            assert SID_B in app.state.expanded and SID_A not in app.state.expanded

    asyncio.run(go())


def test_expanded_state_is_bounded(tmp_path):
    path = tmp_path / "snapshot.json"
    write_snapshot(path, snapshot())

    async def go():
        app = FafoApp(path, refresh_s=0.25)
        async with app.run_test(size=(144, 40)) as pilot:
            await wait_read(pilot, app)
            for i in range(tui.MAX_EXPANDED + 50):
                app.state.toggle_expanded(f"sid-{i}")
            assert len(app.state.expanded) == tui.MAX_EXPANDED
            assert "sid-0" not in app.state.expanded
            assert f"sid-{tui.MAX_EXPANDED + 49}" in app.state.expanded

    asyncio.run(go())


def test_drill_in_has_summary_row_and_expands_with_enter_and_x(tmp_path):
    path = tmp_path / "snapshot.json"
    write_snapshot(path, snapshot())

    async def go():
        app = FafoApp(path, refresh_s=0.25)
        async with app.run_test(size=(144, 40)) as pilot:
            await wait_read(pilot, app)
            select(app, "sess", SID_A)
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert app.view_idx == tui.V_DRILL
            text = "\n".join(lines(app))
            assert "children (4)" in text and "+3 helpers (sleep x3)" in text
            assert "sleep 5" not in text
            for key in ("enter", "x"):
                select(app, "helpers", SID_A)
                await pilot.press(key)
                await pilot.pause(0.1)
                assert "\n".join(lines(app)).count("sleep 5") == 3, key
                select(app, "helpers", SID_A)
                await pilot.press(key)
                await pilot.pause(0.1)
                assert "sleep 5" not in "\n".join(lines(app)), key
            assert app.view_idx == tui.V_DRILL and not app.state.expanded

    asyncio.run(go())


def test_enter_and_x_toggle_the_summary_row_on_the_process_page(tmp_path):
    path = tmp_path / "snapshot.json"
    write_snapshot(path, snapshot())

    async def go():
        app = FafoApp(path, refresh_s=0.25)
        async with app.run_test(size=(144, 40)) as pilot:
            await wait_read(pilot, app)
            await pilot.press("2")
            await pilot.pause(0.1)
            assert app.view_idx == tui.V_PROCS
            assert "sleep 5" not in "\n".join(lines(app))
            assert "+3 helpers (sleep x3)" in "\n".join(lines(app))
            for key in ("enter", "x"):
                select(app, "helpers", SID_A)
                await pilot.press(key)
                await pilot.pause(0.1)
                text = "\n".join(lines(app))
                assert app.view_idx == tui.V_PROCS, key  # Enter did not open the drill-in
                assert text.count("sleep 5") == 3, key  # session A only; B stays collapsed
                assert SID_A in app.state.expanded and SID_B not in app.state.expanded
                select(app, "helpers", SID_A)
                await pilot.press(key)
                await pilot.pause(0.1)
                assert "sleep 5" not in "\n".join(lines(app)), key
                assert not app.state.expanded

    asyncio.run(go())


def big_snapshot(n):
    procs = [proc(5000 + i, 100, "sleep 5") for i in range(n)] + [proc(1200, 100, "pytest -q")]
    return Snapshot(
        version=SNAPSHOT_VERSION, taken=time.time(), host={},
        sessions=[sess(100, SID_A, "src-a")], procs=procs,
    )


def test_hidden_helpers_are_counted_in_process_page_and_drill_in(tmp_path):
    path = tmp_path / "snapshot.json"
    write_snapshot(path, big_snapshot(300))

    async def go():
        app = FafoApp(path, refresh_s=0.25)
        async with app.run_test(size=(144, 40)) as pilot:
            await wait_read(pilot, app)
            node = app.views.sessions[0]
            assert node.collapsed == 300 and node.helpers_hidden == 44
            app.state.toggle_expanded(SID_A)
            for page in (
                tui.build_procs_page(app.views, "", app.state.expanded),
                tui.build_sessions_page(app.views, set(), "", app.state.expanded),
            ):
                texts = [str(i.a) for i in page.items if i.kind == "more"]
                assert "+44 more helpers not shown" in texts
            drill = tui.build_drill_page(app.views, SID_A, 120, app.state.expanded)
            assert "+44 more helpers not shown" in [str(i.a).strip() for i in drill.items]
            # collapsed: no hidden-helpers row anywhere
            off = tui.build_procs_page(app.views, "", set())
            assert not any("more helpers" in str(i.a) for i in off.items)

    asyncio.run(go())


@pytest.mark.parametrize("width", [-3, 0, 1, 2, 4, 7, 8, 9, 40, 80, 144])
def test_helper_summary_and_other_text_rows_fit_the_width(tmp_path, width):
    app = FafoApp(tmp_path / "snapshot.json", refresh_s=0.25)
    long = "+300 helpers (" + ", ".join(f"cmd{i} x{i}" for i in range(60)) + ")"
    for kind in ("helpers", "head", "more", "line"):
        for flag in (True, False):  # the twisty differs for an open and a shut head
            item = tui.Item(kind, (kind, "k"), long, "idle" if kind == "line" else flag)
            row = app._row(item, False, [], {}, width)
            assert row.cell_len <= max(0, width), (kind, width, row.cell_len)
    if width < 1:
        return
    short = tui.Item("helpers", ("helpers", "k"), "+3 helpers (sleep x3)")
    if width >= 40:
        assert "+3 helpers (sleep x3)" in app._row(short, False, [], {}, width).plain


@pytest.mark.parametrize("size", [(80, 24), (144, 40)])
def test_helper_rows_in_the_running_pane_fit_the_width(tmp_path, size):
    path = tmp_path / "snapshot.json"
    write_snapshot(path, snapshot())

    async def go():
        app = FafoApp(path, refresh_s=0.25)
        async with app.run_test(size=size) as pilot:
            await wait_read(pilot, app)
            text = lines(app)
            assert any("helpers" in ln for ln in text)
            assert max(len(ln) for ln in text) <= size[0]

    asyncio.run(go())
