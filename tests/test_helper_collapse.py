"""Collapsing boring helper processes: view model, table, config, views.json."""

from __future__ import annotations

import json

import pytest

from fafo.config import ConfigError, load_config
from fafo.model import SNAPSHOT_VERSION, Proc, Session, Snapshot, Stamped
from fafo.render.export import MAX_BYTES, encode_bounded, views_to_dict
from fafo.render.table import format_table
from fafo.render.view import (
    DEFAULT_BORING,
    HelperPolicy,
    build_views,
    helper_summary,
    policy_from_config,
)

NOW = 1_000_000.0
SID = "11111111-aaaa-4aaa-8aaa-000000000001"
SESSION_PID = 100


def sess(pid=SESSION_PID, sid=SID):
    return Session(
        pid=pid, agent="claude", session_id=sid, project=Stamped("alpha", NOW),
        project_rule="cwd-name", task="work", state="busy", branch="main",
        worktree="/work/example", started=NOW - 100, cpu=1.0, rss=1024, name="src-1", address=sid,
    )


def proc(pid, cmd, ppid=1, cpu=0.5, owner=SESSION_PID):
    return Proc(pid, ppid, owner, "claude", cmd, cpu, 2048, NOW - 10)


def snap(procs, sessions=None):
    return Snapshot(
        version=SNAPSHOT_VERSION, taken=NOW, host={},
        sessions=sessions if sessions is not None else [sess()], procs=list(procs),
    )


def mixed_21():
    """6 sleep, 4 bash, 2 tail, 3 cat, 1 grep (16 boring), plus 5 real ones = 21."""
    procs = []
    n = 1000
    for name, count in (("sleep", 6), ("bash", 4), ("tail", 2), ("cat", 3), ("grep", 1)):
        for _ in range(count):
            n += 1
            procs.append(proc(n, f"/usr/bin/{name} 1"))
    for cmd in ("pytest -q", "git status", "ruff check .", "node server.js", "python build.py"):
        n += 1
        procs.append(proc(n, cmd))
    assert len(procs) == 21
    return snap(procs)


def node_of(views):
    return views.sessions[0]


def test_21_mixed_processes_collapse_to_exact_summary():
    node = node_of(build_views(mixed_21(), NOW))
    assert node.collapsed == 16
    assert node.collapsed_names == ["sleep x6", "bash x4", "cat x3"]
    assert len(node.children) == 5
    assert {c.cmd for c in node.children} == {
        "pytest -q", "git status", "ruff check .", "node server.js", "python build.py",
    }
    assert helper_summary(node.collapsed, node.collapsed_names) == (
        "+16 helpers (sleep x6, bash x4, cat x3)"
    )
    assert len(node.helpers) == 16


def test_tie_break_is_alphabetical_and_top_three_only():
    procs = [proc(1001 + i, f"{n} 1") for i, n in enumerate(["wc", "tail", "sh", "env", "cat"])]
    node = node_of(build_views(snap(procs), NOW))
    assert node.collapsed == 5
    assert node.collapsed_names == ["cat x1", "env x1", "sh x1"]


def test_hot_boring_process_stays_visible():
    procs = [proc(1001, "sleep 1"), proc(1002, "sleep 1", cpu=5.0), proc(1003, "sleep 1", cpu=4.9)]
    node = node_of(build_views(snap(procs), NOW))
    assert [c.pid for c in node.children] == [1002]
    assert node.collapsed == 2


def test_boring_parent_of_real_child_stays_visible():
    procs = [
        proc(1001, "bash -c run", ppid=SESSION_PID),
        proc(1002, "python build.py", ppid=1001),
        proc(1003, "bash -c idle", ppid=SESSION_PID),
    ]
    node = node_of(build_views(snap(procs), NOW))
    assert [c.pid for c in node.children] == [1001, 1002]
    assert node.collapsed == 1


def test_boring_parent_of_hot_boring_child_stays_visible_and_cycles_terminate():
    procs = [
        proc(1001, "bash -c a", ppid=1002),  # ppid cycle with 1002
        proc(1002, "bash -c b", ppid=1001, cpu=50.0),
    ]
    node = node_of(build_views(snap(procs), NOW))
    assert [c.pid for c in node.children] == [1001, 1002]
    assert node.collapsed == 0


def test_path_and_arguments_do_not_fool_the_match():
    procs = [proc(1001, "/tmp/sleep-evil --x"), proc(1002, "python sleep.py"), proc(1003, "")]
    node = node_of(build_views(snap(procs), NOW))
    assert node.collapsed == 0
    assert len(node.children) == 3


def test_policy_off_or_custom_set():
    s = snap([proc(1001, "sleep 1"), proc(1002, "make all")])
    off = node_of(build_views(s, NOW, helpers=HelperPolicy(collapse=False)))
    assert off.collapsed == 0 and len(off.children) == 2 and off.helpers == []
    custom = node_of(build_views(s, NOW, helpers=HelperPolicy(boring=frozenset({"make"}))))
    assert custom.collapsed == 1 and [c.pid for c in custom.children] == [1001]


def test_no_helpers_means_no_summary_fields_set():
    node = node_of(build_views(snap([proc(1001, "make all")]), NOW))
    assert node.collapsed == 0 and node.collapsed_names == [] and node.helpers == []


def test_bounded_sizes_with_count_exact():
    procs = [proc(2000 + i, "sleep 1") for i in range(600)] + [
        proc(5000 + i, f"job{i}") for i in range(300)
    ]
    node = node_of(build_views(snap(procs), NOW))
    assert node.collapsed == 600  # exact, even above the list cap
    assert len(node.helpers) == 256 and node.helpers_hidden == 344
    assert len(node.children) == 256 and node.children_hidden == 44
    assert len(node.collapsed_names) == 1


def test_table_prints_summary_row():
    out = format_table(build_views(mixed_21(), NOW), 120)
    assert "+16 helpers (sleep x6, bash x4, cat x3)" in out
    assert "sleep 1" not in out
    assert "pytest -q" in out


def test_views_json_carries_summary_and_not_the_helper_rows():
    doc = views_to_dict(build_views(mixed_21(), NOW))
    s = doc["sessions"][0]
    assert s["collapsed"] == 16
    assert s["collapsed_names"] == ["sleep x6", "bash x4", "cat x3"]
    assert "helpers" not in s and "helpers_hidden" not in s
    assert doc["schema"] == 2
    assert len(s["children"]) == 5


def test_views_json_stays_within_bound_with_many_collapsed():
    sessions = [sess(10 + i, f"sid-{i}") for i in range(200)]
    procs = [proc(100000 + i * 300 + j, "sleep 1", owner=10 + i) for i in range(200) for j in range(300)]
    views = build_views(snap(procs, sessions), NOW)
    payload = encode_bounded(views_to_dict(views))
    assert len(payload) <= MAX_BYTES
    json.loads(payload)


# ---- config ----------------------------------------------------------------------------------


def cfg(tmp_path, text):
    p = tmp_path / "fafo.toml"
    p.write_text(text, encoding="utf-8")
    return load_config(p)


def test_config_defaults(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    c = load_config(None)
    assert c.collapse_helpers is True
    assert tuple(c.boring_procs) == DEFAULT_BORING
    pol = policy_from_config(c)
    assert pol.collapse and pol.boring == frozenset(DEFAULT_BORING)


def test_config_accepts_valid_values(tmp_path):
    c = cfg(tmp_path, 'collapse_helpers = false\nboring_procs = ["sleep", "my-tool_1.x"]\n')
    assert c.collapse_helpers is False
    assert list(c.boring_procs) == ["sleep", "my-tool_1.x"]


SECRETISH = "ZZtopsecretvalueZZ"


@pytest.mark.parametrize(
    "text, option",
    [
        (f'collapse_helpers = "{SECRETISH}"\n', "collapse_helpers"),
        ("collapse_helpers = 1\n", "collapse_helpers"),
        (f'boring_procs = "{SECRETISH}"\n', "boring_procs"),
        (f'boring_procs = [1, "{SECRETISH}"]\n', "boring_procs"),
        (f'boring_procs = ["a b{SECRETISH}"]\n', "boring_procs"),
        (f'boring_procs = ["/bin/{SECRETISH}"]\n', "boring_procs"),
        ('boring_procs = [""]\n', "boring_procs"),
        (f'boring_procs = ["{"x" * 33}"]\n', "boring_procs"),
        ("boring_procs = [" + ",".join(f'"p{i}"' for i in range(65)) + "]\n", "boring_procs"),
    ],
)
def test_config_rejects_bad_values_naming_option_without_echo(tmp_path, text, option):
    with pytest.raises(ConfigError) as exc:
        cfg(tmp_path, text)
    assert option in str(exc.value)
    assert SECRETISH not in str(exc.value)


def test_config_accepts_exactly_64_entries_of_32_chars(tmp_path):
    names = [f"{i:02d}" + "x" * 30 for i in range(64)]
    c = cfg(tmp_path, "boring_procs = [" + ",".join(f'"{n}"' for n in names) + "]\n")
    assert len(c.boring_procs) == 64
