"""TUI: the helper summary row, and the per-session expand toggle."""

from __future__ import annotations

import asyncio
import time

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


def test_drill_in_lists_every_process_including_helpers(tmp_path):
    path = tmp_path / "snapshot.json"
    write_snapshot(path, snapshot())

    async def go():
        app = FafoApp(path, refresh_s=0.25)
        async with app.run_test(size=(144, 40)) as pilot:
            await wait_read(pilot, app)
            select(app, "sess", SID_A)
            await pilot.press("enter")
            await pilot.pause(0.1)
            text = "\n".join(lines(app))
            assert "children (4)" in text and text.count("sleep 5") == 3

    asyncio.run(go())
