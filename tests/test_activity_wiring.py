"""Activity wired through the Codex source, store, config, view, table and export."""

import json
import logging
from pathlib import Path

import pytest
from test_sources_codex import FakeProc

from fafo.activity import ActivityTracker
from fafo.config import ConfigError, SourceCfg, load_config
from fafo.model import SNAPSHOT_VERSION, Proc, Session, Snapshot, Stamped
from fafo.render.export import views_to_dict
from fafo.render.table import format_table
from fafo.render.view import build_views
from fafo.source import Ctx
from fafo.sources.codex import CodexSource
from fafo.store import read_snapshot, write_snapshot

NOW = 1_000_000.0


class Times:
    def __init__(self, user, system=0.0):
        self.user, self.system = user, system


class CpuProc(FakeProc):
    def __init__(self, *a, user=0.0, system=0.0, **k):
        super().__init__(*a, **k)
        self.t = (user, system)

    def cpu_times(self):
        return Times(*self.t)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def ctx():
    return Ctx(cfg=SourceCfg(), clock=lambda: 0.0, log=logging.getLogger("t"))


def test_source_reports_active_then_idle_by_cpu_change():
    clock = Clock()
    p = CpuProc(10, 1, ["codex", "exec", "x"], user=1.0)
    src = CodexSource(lister=lambda: [p], tracker=ActivityTracker(clock=clock))
    assert src.collect(ctx()).procs[0].activity == "unknown"
    clock.t += 2.0
    p.t = (1.5, 0.2)
    assert src.collect(ctx()).procs[0].activity == "active"
    for _ in range(4):  # the window (4 samples) must drain the busy period first
        clock.t += 2.0
        last = src.collect(ctx()).procs[0].activity
    assert last == "idle"


def test_source_handle_without_cpu_times_is_unknown():
    src = CodexSource(lister=lambda: [FakeProc(10, 1, ["codex", "exec", "x"])])
    assert src.collect(ctx()).procs[0].activity == "unknown"


def test_source_pid_reuse_resets():
    clock = Clock()
    cur = [CpuProc(10, 1, ["codex", "exec", "x"], user=5.0, started=100.0)]
    src = CodexSource(lister=lambda: cur, tracker=ActivityTracker(clock=clock))
    src.collect(ctx())
    clock.t += 2.0
    cur[0] = CpuProc(10, 1, ["codex", "exec", "x"], user=5.0, started=500.0)
    assert src.collect(ctx()).procs[0].activity == "unknown"


def test_source_hostile_cpu_times_never_raise():
    class Bad(CpuProc):
        def cpu_times(self):
            return Times(float("nan"), -5.0)

    src = CodexSource(lister=lambda: [Bad(10, 1, ["codex", "exec", "x"])])
    assert src.collect(ctx()).procs[0].activity == "unknown"

    class Raises(CpuProc):
        def cpu_times(self):
            raise OverflowError

    src = CodexSource(lister=lambda: [Raises(10, 1, ["codex", "exec", "x"])])
    assert src.collect(ctx()).procs[0].activity == "unknown"


def _snap(activity):
    return Snapshot(
        version=SNAPSHOT_VERSION, taken=NOW, host={},
        sessions=[Session(
            pid=7, agent="codex", session_id="s1", project=Stamped("p", NOW), project_rule="none",
            task="codex", state="unknown", branch="", worktree="", started=NOW - 60, cpu=1.0,
            rss=1, activity=activity)],
        procs=[Proc(pid=8, ppid=1, session_pid=None, agent="codex", cmd="codex exec", cpu=2.0,
                    rss=1, started=NOW - 60, activity=activity)],
    )


def test_store_roundtrip_and_unknown_key_omitted(tmp_path: Path):
    p = tmp_path / "s.json"
    write_snapshot(p, _snap("active"))
    got = read_snapshot(p)
    assert got.sessions[0].activity == "active" and got.procs[0].activity == "active"
    write_snapshot(p, _snap("unknown"))
    doc = json.loads(p.read_text())
    assert "activity" not in doc["sessions"][0] and "activity" not in doc["procs"][0]
    assert read_snapshot(p).procs[0].activity == "unknown"


def test_store_old_snapshot_loads_unknown_and_bad_value_refused(tmp_path: Path):
    p = tmp_path / "s.json"
    write_snapshot(p, _snap("idle"))
    doc = json.loads(p.read_text())
    for part in ("sessions", "procs"):
        doc[part][0].pop("activity")
    p.write_text(json.dumps(doc))
    got = read_snapshot(p)
    assert got.sessions[0].activity == "unknown" and got.procs[0].activity == "unknown"
    for part in ("sessions", "procs"):
        doc[part][0]["activity"] = "busy"
        p.write_text(json.dumps(doc))
        assert read_snapshot(p) is None
        doc[part][0].pop("activity")


def test_snapshot_version_unchanged():
    assert SNAPSHOT_VERSION == 2


def _flat(snap):
    return [n for g in snap.unattached for n in g.procs] + [
        n for g in snap.unattached for n in g.sessions] + list(snap.sessions)


def test_view_table_and_export():
    views = build_views(_snap("active"), now=NOW)
    assert "active" in {getattr(n, "activity", None) for n in _flat(views)}
    text = format_table(views, 140)
    assert "active" in text
    idle = format_table(build_views(_snap("idle"), now=NOW), 140)
    assert "idle" in idle and "active" not in idle
    unk = format_table(build_views(_snap("unknown"), now=NOW), 140)
    assert "idle" not in unk and "active" not in unk
    doc = views_to_dict(views)
    assert json.dumps(doc)
    assert '"activity":"active"' in json.dumps(doc, separators=(",", ":"))


def test_hostile_activity_value_in_view_is_unknown():
    s = _snap("active")
    s.procs[0].activity = "<b>red</b>"
    s.sessions[0].activity = "<b>red</b>"
    for n in _flat(build_views(s, now=NOW)):
        assert getattr(n, "activity", "unknown") == "unknown"


@pytest.mark.parametrize("lit,ok", [("0.5", True), ("1", True), ("100", True), ("0.4", False),
                                    ("101", False), ("true", False), ('"1"', False), ("nan", False)])
def test_config_option(tmp_path: Path, lit, ok):
    f = tmp_path / "c.toml"
    f.write_text(f"[sources.codex]\nactive_cpu_pct = {lit}\n")
    if ok:
        assert load_config(f).sources["codex"].options["active_cpu_pct"] == float(lit)
    else:
        with pytest.raises(ConfigError):
            load_config(f)
