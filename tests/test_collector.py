import threading
import time
from pathlib import Path

import pytest

from fafo.collector import Collector, backoff_delay
from fafo.config import Config, SourceCfg, load_config
from fafo.model import Inflight, LlmServer, Proc, Runner, Session, Stamped
from fafo.source import Ctx, SourceResult
from fafo.store import read_snapshot, write_snapshot


def make_cfg(tmp_path: Path, **srcs: SourceCfg) -> Config:
    return Config(
        sources=dict(srcs), aliases={}, snapshot_path=tmp_path / "s.json", show_args=set()
    )


def fast(timeout_s=0.3) -> SourceCfg:
    return SourceCfg(enabled=True, interval_s=0.1, timeout_s=timeout_s)


def session(pid: int, as_of: float = 100.0) -> Session:
    return Session(
        pid=pid, agent="claude", session_id=f"id{pid}", project=Stamped("p", as_of),
        project_rule="cwd-name", task="t", state="busy", branch="b", worktree="w",
        started=1.0, cpu=0.0, rss=1,
    )


class Counter:
    name = "counter"

    def __init__(self):
        self.n = 0

    def collect(self, ctx: Ctx) -> SourceResult:
        self.n += 1
        return SourceResult(sessions=[session(self.n)])


class Hung:
    name = "hung"

    def __init__(self, release: threading.Event):
        self.release = release

    def collect(self, ctx: Ctx) -> SourceResult:
        self.release.wait(30)
        return SourceResult()


class Flaky:
    name = "flaky"

    def __init__(self):
        self.fail = True

    def collect(self, ctx: Ctx) -> SourceResult:
        if self.fail:
            raise RuntimeError("boom")
        return SourceResult(procs=[Proc(1, 0, None, "x", "c", 0.0, 1, 1.0)])


class Running:
    """Runs a collector in a thread; always stops it and releases hung sources."""

    def __init__(self, col: Collector, release: threading.Event | None = None):
        self.col, self.release = col, release
        self.thread = threading.Thread(target=col.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.col

    def __exit__(self, *exc):
        self.col.stop()
        if self.release is not None:
            self.release.set()
        self.thread.join(5)
        assert not self.thread.is_alive()


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def health(snap, name):
    return next(h for h in snap.health if h.name == name)


def test_hung_source_does_not_delay_others(tmp_path):
    release = threading.Event()
    cfg = make_cfg(tmp_path, counter=fast(), hung=fast())
    col = Collector(cfg, [Counter(), Hung(release)], cfg.snapshot_path)
    with Running(col, release):
        s1 = wait_for(lambda: read_snapshot(cfg.snapshot_path) if col.snapshot().sessions else None)
        first = s1.sessions[0].pid if s1.sessions else 0
        wait_for(lambda: (r := read_snapshot(cfg.snapshot_path)) and r.sessions
                 and r.sessions[0].pid > first + 3)
        snap = wait_for(lambda: (r := read_snapshot(cfg.snapshot_path))
                        and health(r, "hung").status == "stale"
                        and "timeout" in health(r, "hung").error and r)
        assert health(snap, "counter").status == "ok"


def test_crashing_source_marked_down_and_recovers(tmp_path):
    src = Flaky()
    cfg = make_cfg(tmp_path, flaky=fast())
    col = Collector(cfg, [src], cfg.snapshot_path)
    with Running(col):
        snap = wait_for(lambda: (r := read_snapshot(cfg.snapshot_path))
                        and health(r, "flaky").status == "down" and r)
        assert "boom" in health(snap, "flaky").error
        src.fail = False
        snap = wait_for(lambda: (r := read_snapshot(cfg.snapshot_path))
                        and health(r, "flaky").status == "ok" and r)
        assert health(snap, "flaky").error == "" and len(snap.procs) == 1


def test_stale_rows_keep_old_as_of(tmp_path):
    class Once:
        name = "once"
        calls = 0

        def collect(self, ctx):
            Once.calls += 1
            if Once.calls == 1:
                return SourceResult(sessions=[session(7, as_of=123.0)])
            raise RuntimeError("gone")

    cfg = make_cfg(tmp_path, once=fast())
    col = Collector(cfg, [Once()], cfg.snapshot_path)
    with Running(col):
        snap = wait_for(lambda: (r := read_snapshot(cfg.snapshot_path))
                        and health(r, "once").status == "down" and r)
        assert [s.pid for s in snap.sessions] == [7]
        assert snap.sessions[0].project.as_of == 123.0


def test_snapshot_written_every_tick_even_if_all_sources_hung(tmp_path):
    release = threading.Event()
    cfg = make_cfg(tmp_path, hung=fast())
    col = Collector(cfg, [Hung(release)], cfg.snapshot_path)
    with Running(col, release):
        taken = set()
        end = time.monotonic() + 3
        while len(taken) < 4 and time.monotonic() < end:
            r = read_snapshot(cfg.snapshot_path)
            if r:
                taken.add(r.taken)
            time.sleep(0.03)
        assert len(taken) >= 4
        wait_for(lambda: "timeout" in health(read_snapshot(cfg.snapshot_path), "hung").error)
        assert health(read_snapshot(cfg.snapshot_path), "hung").status == "stale"


def test_backoff_caps_at_60s(tmp_path):
    now = [1000.0]
    src = Flaky()
    cfg = make_cfg(tmp_path, flaky=SourceCfg(True, 2.0, 1.0))
    col = Collector(cfg, [src], cfg.snapshot_path, clock=lambda: now[0])
    st = col._states[0]
    delays = []
    for _ in range(8):
        col._poll(st)
        delays.append(st.delay)
        now[0] += st.delay  # fake time passes; no real sleeping
    assert delays[:5] == [4.0, 8.0, 16.0, 32.0, 60.0]
    assert max(delays) == 60.0 and delays[-1] == 60.0
    assert st.last_ok == 0.0
    src.fail = False
    col._poll(st)
    assert st.delay == 2.0 and st.last_ok == now[0]
    assert backoff_delay(2.0, 1000) == 60.0
    assert backoff_delay(120.0, 5) == 120.0


def test_stop_returns_promptly_with_hung_source(tmp_path):
    release = threading.Event()
    cfg = make_cfg(tmp_path, hung=fast(timeout_s=10), counter=fast())
    col = Collector(cfg, [Hung(release), Counter()], cfg.snapshot_path)
    with Running(col, release):
        wait_for(lambda: read_snapshot(cfg.snapshot_path))
        time.sleep(0.2)
        t0 = time.monotonic()
        col.stop()
        assert time.monotonic() - t0 < 2.0


def test_error_text_is_sanitized_and_capped(tmp_path):
    class Evil:
        name = "evil"

        def collect(self, ctx):
            raise RuntimeError("\x1b]0;pwn\x07bad\x00\n" + "x" * 1000)

    cfg = make_cfg(tmp_path, evil=fast())
    col = Collector(cfg, [Evil()], cfg.snapshot_path)
    with Running(col):
        snap = wait_for(lambda: (r := read_snapshot(cfg.snapshot_path))
                        and health(r, "evil").status == "down" and r)
        err = health(snap, "evil").error
        assert len(err) <= 200
        assert all(c.isprintable() for c in err)
        assert "\x1b" not in err and "\x07" not in err


def test_absent_source_is_hidden_not_error(tmp_path):
    class Gone:
        name = "gone"

        def collect(self, ctx):
            return SourceResult(status="absent")

    cfg = make_cfg(tmp_path, gone=fast())
    col = Collector(cfg, [Gone()], cfg.snapshot_path)
    st = col._states[0]
    col._poll(st)
    h = health(col.snapshot(), "gone")
    assert h.status == "absent" and h.error == ""
    assert st.failures == 0 and st.delay == 0.1


def alias_cfg(tmp_path: Path, **srcs: SourceCfg) -> Config:
    f = tmp_path / "c.toml"
    f.write_text('[aliases]\n"build-host.invalid" = "runner-a"\n')
    base = load_config(f)
    return Config(
        sources=dict(srcs), aliases=base.aliases, snapshot_path=tmp_path / "s.json",
        show_args=set(), _alias_re=base._alias_re, _alias_map=base._alias_map,
    )


def test_rows_are_scrubbed_and_aliased_at_the_collector(tmp_path):
    h = "build-host.invalid"
    tok = "ghp_" + "a1B2" * 8
    esc = chr(0x1B)
    s = session(1)
    s.task = "do" + esc + "]0;pwn" + chr(7) + " it on " + h
    s.branch = "br " + esc + "[31m" + h
    s.worktree = h
    s.project = Stamped("proj " + h, 5.0)

    class Raw:
        name = "raw"

        def collect(self, ctx):
            return SourceResult(
                sessions=[s],
                procs=[Proc(2, 1, None, "x", f"tool {esc}[2J --opt {tok} {h}", 0.0, 1, 1.0)],
                runners=[Runner(h, "idle", h, "w", "j", "s" + chr(0), 1, 2, 0.0, 0.0, 0.0, 0.0)],
                llm=LlmServer([], 0, [Inflight(h, None, 1.0)], {h: h}),
                host={h: "up on " + h},
                quota={"k" + esc + "[0m": h},
            )

    cfg = alias_cfg(tmp_path, raw=fast())
    col = Collector(cfg, [Raw()], cfg.snapshot_path)
    col._poll(col._states[0])
    snap = col.snapshot()
    blob = repr(snap)
    assert h not in blob and tok not in blob
    assert "\\x1b" not in blob and "\\x07" not in blob and "\\x00" not in blob
    ss = snap.sessions[0]
    assert ss.task == "do it on runner-a" and ss.branch == "br runner-a"
    assert ss.worktree == "runner-a" and ss.project.value == "proj runner-a"
    assert ss.project.as_of == 5.0
    assert snap.runners[0].name == "runner-a" and snap.host == {"runner-a": "up on runner-a"}
    assert snap.llm.inflight[0].model == "runner-a" and snap.quota == {"k": "runner-a"}
    assert snap.procs[0].cmd.startswith("tool") and "<redacted>" in snap.procs[0].cmd


def test_error_text_strips_unicode_separators_and_bidi(tmp_path):
    msg = "a" + chr(0x2028) + "b" + chr(0x200B) + "c" + chr(0x202E) + "d\r\ne"

    class Evil:
        name = "evil"

        def collect(self, ctx):
            raise RuntimeError(msg)

    cfg = make_cfg(tmp_path, evil=fast())
    col = Collector(cfg, [Evil()], cfg.snapshot_path)
    col._poll(col._states[0])
    err = health(col.snapshot(), "evil").error
    for bad in (chr(0x2028), chr(0x200B), chr(0x202E)):
        assert bad not in err
    assert "RuntimeError: abcd  e" in err


def test_invalid_status_marks_down_keeps_rows_and_snapshot_readable(tmp_path):
    class Bogus:
        name = "bogus"
        n = 0

        def collect(self, ctx):
            Bogus.n += 1
            if Bogus.n == 1:
                return SourceResult(sessions=[session(9)])
            return SourceResult(sessions=[session(10)], status="bogus")

    cfg = make_cfg(tmp_path, bogus=fast())
    col = Collector(cfg, [Bogus()], cfg.snapshot_path)
    st = col._states[0]
    col._poll(st)
    col._poll(st)
    snap = col.snapshot()
    h = health(snap, "bogus")
    assert h.status == "down" and h.error == "invalid status from source"
    assert [x.pid for x in snap.sessions] == [9]
    assert st.failures == 1 and st.delay == 0.2
    write_snapshot(cfg.snapshot_path, snap)
    assert read_snapshot(cfg.snapshot_path) is not None


def test_disabled_source_not_run(tmp_path):
    cfg = make_cfg(tmp_path, counter=SourceCfg(False, 0.1, 0.3))
    col = Collector(cfg, [Counter()], cfg.snapshot_path)
    assert col.snapshot().health == []


def test_garbage_result_marks_down(tmp_path):
    class Garbage:
        name = "garbage"

        def collect(self, ctx):
            return "not a result"

    cfg = make_cfg(tmp_path, garbage=fast())
    col = Collector(cfg, [Garbage()], cfg.snapshot_path)
    col._poll(col._states[0])
    assert health(col.snapshot(), "garbage").status == "down"


@pytest.fixture(autouse=True)
def _no_leaked_threads():
    yield
    end = time.monotonic() + 3
    while time.monotonic() < end:
        if not [t for t in threading.enumerate() if t.name.startswith("fafo-")]:
            return
        time.sleep(0.05)
    raise AssertionError("leaked fafo threads")
