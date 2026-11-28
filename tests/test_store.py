import json
import os
import stat
import threading
from dataclasses import fields
from pathlib import Path

import pytest

from fafo import store
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
from fafo.store import read_snapshot, write_snapshot


@pytest.fixture(autouse=True)
def _private_umask():
    """Hand-written fixture files must be owner-only, like the writer's output; the reader insists."""
    old = os.umask(0o077)
    yield
    os.umask(old)


def make_snapshot(taken: float = 1000.0) -> Snapshot:
    session = Session(
        pid=42,
        agent="claude",
        session_id="abc",
        project=Stamped("demo", 999.5),
        project_rule="cwd-name",
        task="fix tests",
        state="busy",
        branch="main",
        worktree="wt-1",
        started=900.0,
        cpu=1.5,
        rss=1024,
        name="fix-tests",
        address="11111111-2222-3333-4444-555555555555",
    )
    proc = Proc(
        pid=43, ppid=42, session_pid=42, agent="claude", cmd="git status",
        cpu=0.1, rss=2048, started=950.0,
    )
    llm = LlmServer(
        models=[LoadedModel(name="m", vram=100, context=8192, expires=1200.0)],
        queue=1,
        inflight=[Inflight(model="m", session_pid=42, started=990.0)],
        gpu={"util": 78},
    )
    runner = Runner(
        name="r1", state="busy", repo="o/r", workflow="ci", job="build", step="test",
        step_n=3, step_total=7, elapsed=130.0, load=3.2, mem=0.5, disk=0.4,
    )
    health = SourceHealth(name="claude", status="ok", last_ok=taken, error="")
    return Snapshot(
        version=SNAPSHOT_VERSION,
        taken=taken,
        host={"load": 4.1},
        sessions=[session],
        procs=[proc],
        llms=[llm],
        runners=[runner],
        quota={"claude": {"5h": 0.41}},
        health=[health],
    )


def test_write_is_0600(tmp_path: Path):
    path = tmp_path / "snap.json"
    write_snapshot(path, make_snapshot())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["snap.json"]


def test_roundtrip_equal(tmp_path: Path):
    path = tmp_path / "snap.json"
    snap = make_snapshot()
    write_snapshot(path, snap)
    assert read_snapshot(path) == snap


def test_roundtrip_without_llm(tmp_path: Path):
    path = tmp_path / "snap.json"
    snap = make_snapshot()
    snap = Snapshot(**{**snap.__dict__, "llms": []})
    write_snapshot(path, snap)
    assert read_snapshot(path) == snap


def test_read_missing_returns_none(tmp_path: Path):
    assert read_snapshot(tmp_path / "nope.json") is None


def test_read_truncated_returns_none(tmp_path: Path):
    path = tmp_path / "snap.json"
    write_snapshot(path, make_snapshot())
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    assert read_snapshot(path) is None


def test_read_garbage_and_wrong_shape_return_none(tmp_path: Path):
    path = tmp_path / "snap.json"
    for content in (b"", b"\xff\xfe", b"[]", b"null", b'{"version": 1}', b'{"version": "x"}'):
        path.write_bytes(content)
        assert read_snapshot(path) is None


def test_unknown_major_version_returns_none(tmp_path: Path):
    path = tmp_path / "snap.json"
    write_snapshot(path, make_snapshot())
    doc = json.loads(path.read_text())
    doc["version"] = 3
    path.write_text(json.dumps(doc))
    assert read_snapshot(path) is None
    doc["version"] = 2
    path.write_text(json.dumps(doc))
    assert read_snapshot(path) is not None


def test_read_directory_returns_none(tmp_path: Path):
    assert read_snapshot(tmp_path) is None


def test_concurrent_read_never_partial(tmp_path: Path):
    path = tmp_path / "snap.json"
    write_snapshot(path, make_snapshot(0.0))
    stop = threading.Event()
    results: list[object] = []

    def reader() -> None:
        while not stop.is_set():
            results.append(read_snapshot(path))

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(200):
            write_snapshot(path, make_snapshot(float(i)))
    finally:
        stop.set()
        t.join()
    assert results
    assert all(r is None or isinstance(r, Snapshot) for r in results)
    assert any(isinstance(r, Snapshot) for r in results)


def _doc(tmp_path: Path) -> tuple[Path, dict]:
    path = tmp_path / "snap.json"
    write_snapshot(path, make_snapshot())
    return path, json.loads(path.read_text())


def _mutations():
    def setter(*keys, value):
        def apply(doc):
            node = doc
            for k in keys[:-1]:
                node = node[k]
            node[keys[-1]] = value

        return apply

    cases = {
        "session_pid_str": setter("sessions", 0, "pid", value="not-an-int"),
        "session_pid_bool": setter("sessions", 0, "pid", value=True),
        "session_pid_negative": setter("sessions", 0, "pid", value=-1),
        "session_pid_float": setter("sessions", 0, "pid", value=1.5),
        "session_cpu_str": setter("sessions", 0, "cpu", value="1.5"),
        "session_cpu_nan": setter("sessions", 0, "cpu", value=float("nan")),
        "session_cpu_inf": setter("sessions", 0, "cpu", value=float("inf")),
        "session_agent_none": setter("sessions", 0, "agent", value=None),
        "session_name_none": setter("sessions", 0, "name", value=None),
        "session_address_int": setter("sessions", 0, "address", value=7),
        "session_rss_negative": setter("sessions", 0, "rss", value=-5),
        "session_project_not_dict": setter("sessions", 0, "project", value="demo"),
        "session_project_value_int": setter("sessions", 0, "project", "value", value=3),
        "session_project_as_of_str": setter("sessions", 0, "project", "as_of", value="x"),
        "proc_pid_str": setter("procs", 0, "pid", value="43"),
        "proc_session_pid_str": setter("procs", 0, "session_pid", value="42"),
        "proc_cmd_none": setter("procs", 0, "cmd", value=None),
        "proc_started_nan": setter("procs", 0, "started", value=float("nan")),
        "runner_step_n_bool": setter("runners", 0, "step_n", value=False),
        "runner_load_str": setter("runners", 0, "load", value="3.2"),
        "runner_name_int": setter("runners", 0, "name", value=1),
        "health_status_bad": setter("health", 0, "status", value="broken"),
        "health_status_int": setter("health", 0, "status", value=1),
        "health_last_ok_inf": setter("health", 0, "last_ok", value=float("inf")),
        "health_error_none": setter("health", 0, "error", value=None),
        "llm_model_name_int": setter("llm", "models", 0, "name", value=7),
        "llm_model_vram_str": setter("llm", "models", 0, "vram", value="100"),
        "llm_model_expires_nan": setter("llm", "models", 0, "expires", value=float("nan")),
        "llm_inflight_model_none": setter("llm", "inflight", 0, "model", value=None),
        "llm_inflight_pid_str": setter("llm", "inflight", 0, "session_pid", value="42"),
        "llm_queue_str": setter("llm", "queue", value="1"),
        "llm_queue_negative": setter("llm", "queue", value=-1),
        "llm_models_not_list": setter("llm", "models", value={}),
        "llm_gpu_not_dict": setter("llm", "gpu", value=[]),
        "sessions_not_list": setter("sessions", value={}),
        "procs_not_list": setter("procs", value="x"),
        "runners_not_list": setter("runners", value=None),
        "health_not_list": setter("health", value=1),
        "host_not_dict": setter("host", value=[1]),
        "quota_not_dict": setter("quota", value="x"),
        "taken_str": setter("taken", value="1000"),
        "taken_nan": setter("taken", value=float("nan")),
        "version_bool": setter("version", value=True),
    }
    return cases


def test_valid_doc_unmutated_is_accepted(tmp_path: Path):
    path, _ = _doc(tmp_path)
    assert read_snapshot(path) is not None


# INTENDED CHANGE (task76): a bad typed NUMBER no longer refuses the whole file. These cases used to
# return None; now the bad number becomes its default (row kept) or, for an identity field (pid,
# session id), the one row is dropped. Everything else below still refuses the file.
REPAIRED_OR_DROPPED = {
    "session_pid_str", "session_pid_bool", "session_pid_negative", "session_pid_float",
    "session_cpu_str", "session_cpu_nan", "session_cpu_inf", "session_rss_negative",
    "session_project_as_of_str", "proc_pid_str", "proc_session_pid_str", "proc_started_nan",
    "runner_step_n_bool", "runner_load_str", "health_last_ok_inf", "llm_model_vram_str",
    "llm_model_expires_nan", "llm_inflight_pid_str", "llm_queue_str", "llm_queue_negative",
    "taken_str", "taken_nan", "runner_name_int",
}


def test_wrong_types_return_none(tmp_path: Path):
    cases = _mutations()
    assert len(cases) > 30
    strict = {n: a for n, a in cases.items() if n not in REPAIRED_OR_DROPPED}
    assert len(strict) > 15 and len(cases) - len(strict) == len(REPAIRED_OR_DROPPED)
    for name, apply in strict.items():
        path, doc = _doc(tmp_path)
        apply(doc)
        # json.dumps emits NaN/Infinity tokens, which json.loads accepts: the validator must reject
        path.write_text(json.dumps(doc))
        assert read_snapshot(path) is None, name


def test_bad_typed_numbers_are_repaired_or_the_row_dropped_never_the_file(tmp_path: Path):
    cases = _mutations()
    for name in sorted(REPAIRED_OR_DROPPED):
        path, doc = _doc(tmp_path)
        cases[name](doc)
        doc["llms"] = [doc["llm"]]  # the writer keeps the "llm" copy and llms[0] in step
        path.write_text(json.dumps(doc))
        snap = read_snapshot(path)
        assert snap is not None, name
        _assert_well_typed(snap)


def test_int_accepted_for_float_fields(tmp_path: Path):
    path, doc = _doc(tmp_path)
    doc["taken"] = 1000
    doc["sessions"][0]["cpu"] = 2
    path.write_text(json.dumps(doc))
    snap = read_snapshot(path)
    assert snap is not None and snap.sessions[0].cpu == 2


def test_replace_is_atomic_reader_sees_old_snapshot(tmp_path: Path):
    path = tmp_path / "snap.json"
    old = make_snapshot(1.0)
    write_snapshot(path, old)
    seen: list[object] = []

    def hook() -> None:
        seen.append(read_snapshot(path))

    write_snapshot(path, make_snapshot(2.0), _before_replace=hook)
    assert seen == [old]
    after = read_snapshot(path)
    assert after is not None and after.taken == 2.0


def test_write_creates_missing_parent_dirs_0700(tmp_path: Path):
    target = tmp_path / "a" / "b" / "snap.json"
    write_snapshot(target, make_snapshot())
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    for d in (tmp_path / "a", tmp_path / "a" / "b"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    write_snapshot(target, make_snapshot(2000.0))
    assert read_snapshot(target).taken == 2000.0


def test_write_never_chmods_existing_directory(tmp_path: Path):
    d = tmp_path / "shared"
    d.mkdir()
    d.chmod(0o755)
    write_snapshot(d / "snap.json", make_snapshot())
    assert stat.S_IMODE(d.stat().st_mode) == 0o755


def test_session_name_and_address_roundtrip(tmp_path: Path):
    path = tmp_path / "snap.json"
    write_snapshot(path, make_snapshot())
    got = read_snapshot(path)
    assert got is not None
    assert got.sessions[0].name == "fix-tests"
    assert got.sessions[0].address == "11111111-2222-3333-4444-555555555555"


def test_older_snapshot_without_name_and_address_reads_as_empty(tmp_path: Path):
    path, doc = _doc(tmp_path)
    del doc["sessions"][0]["name"]
    del doc["sessions"][0]["address"]
    path.write_text(json.dumps(doc))
    got = read_snapshot(path)
    assert got is not None
    assert got.sessions[0].name == ""
    assert got.sessions[0].address == ""


def test_session_unknown_extra_field_still_rejected(tmp_path: Path):
    path, doc = _doc(tmp_path)
    doc["sessions"][0]["peerToken"] = "x"
    path.write_text(json.dumps(doc))
    assert read_snapshot(path) is None


# ---- non-finite numbers anywhere in the document are rejected -------------------------------

NONFINITE = ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"]
LOCATIONS = [
    ('"load":4.1', '"load":{v}'),  # host
    ('"5h":0.41', '"5h":{v}'),  # quota, nested
    ('"util":78', '"util":{v}'),  # llm.gpu
]


def _tampered(tmp_path: Path, old: str, new: str) -> Path:
    path = tmp_path / "snap.json"
    write_snapshot(path, make_snapshot())
    text = path.read_text(encoding="utf-8")
    # the fixture really contains the value; llm values appear twice ("llm" copies llms[0])
    assert 1 <= text.count(old) <= 2
    path.write_text(text.replace(old, new), encoding="utf-8")
    return path


def test_untampered_fixture_still_reads(tmp_path: Path):
    path = tmp_path / "snap.json"
    write_snapshot(path, make_snapshot())
    assert read_snapshot(path) is not None


def test_nonfinite_numbers_rejected_everywhere(tmp_path: Path):
    for old, template in LOCATIONS:
        for value in NONFINITE:
            path = _tampered(tmp_path, old, template.format(v=value))
            assert read_snapshot(path) is None, (old, value)


def test_deeply_nested_values_are_rejected_not_crashed(tmp_path: Path):
    nest = "[" * 5000 + "1.5" + "]" * 5000
    path = _tampered(tmp_path, '"load":4.1', f'"load":{nest}')
    assert read_snapshot(path) is None


def two_servers() -> Snapshot:
    snap = make_snapshot()
    a = snap.llms[0]
    a.name, a.last_ok = "alpha", 990.0
    b = LlmServer(
        models=[LoadedModel(name="m2", vram=5, context=4096, expires=0.0)],
        queue=3, inflight=[], gpu={}, name="beta", last_ok=0.0, reachable=False,
    )
    return Snapshot(**{**snap.__dict__, "llms": [a, b]})


def test_roundtrip_two_servers_and_llm_is_the_first(tmp_path: Path):
    path = tmp_path / "snap.json"
    snap = two_servers()
    write_snapshot(path, snap)
    got = read_snapshot(path)
    assert got == snap
    assert [s.name for s in got.llms] == ["alpha", "beta"] and not got.llms[1].reachable
    assert got.llm is got.llms[0]
    assert json.loads(path.read_bytes())["llm"]["name"] == "alpha"  # older readers see this
    assert json.loads(path.read_bytes())["version"] == 2


def test_reads_the_old_schema_version_1(tmp_path: Path):
    path = tmp_path / "snap.json"
    doc = {
        "version": 1, "taken": 1000.0, "host": {}, "sessions": [], "procs": [],
        "llm": {"models": [{"name": "m", "vram": 1, "context": 2, "expires": 0.0}],
                "queue": 1, "inflight": [], "gpu": {}},
        "runners": [], "quota": {}, "health": [],
    }
    path.write_text(json.dumps(doc))
    got = read_snapshot(path)
    assert got is not None and [s.name for s in got.llms] == ["ollama"]
    assert got.llm.models[0].name == "m" and got.llm.reachable and got.llm.last_ok == 0.0
    doc["llm"] = None
    path.write_text(json.dumps(doc))
    got = read_snapshot(path)
    assert got is not None and got.llms == [] and got.llm is None


def test_version_1_with_llms_field_is_refused(tmp_path: Path):
    path = tmp_path / "snap.json"
    write_snapshot(path, two_servers())
    doc = json.loads(path.read_bytes())
    doc["version"] = 1
    path.write_text(json.dumps(doc))
    assert read_snapshot(path) is None


@pytest.mark.parametrize(
    "mut",
    [
        # (task76: a bad server NAME drops that server, and a bad last_ok is repaired to 0.0;
        # both used to refuse the file and are covered by the per-row repair tests below.)
        lambda d: d.update(llm=None),
        lambda d: d["llms"][1].update(reachable="yes"),
        lambda d: d["llms"][1].pop("name"),
        lambda d: d.update(llms={}),
        lambda d: d["llms"].extend([d["llms"][0]] * 8),
    ],
)
def test_bad_llms_rejected(tmp_path: Path, mut):
    path = tmp_path / "snap.json"
    write_snapshot(path, two_servers())
    doc = json.loads(path.read_bytes())
    mut(doc)
    path.write_text(json.dumps(doc, allow_nan=True))
    assert read_snapshot(path) is None


def test_project_source_roundtrip_and_old_snapshot_defaults_live(tmp_path: Path):
    path = tmp_path / "snap.json"
    snap = make_snapshot()
    snap.sessions[0].project_source = "last"
    write_snapshot(path, snap)
    got = read_snapshot(path)
    assert got is not None and got.sessions[0].project_source == "last"
    path2, doc = _doc(tmp_path)
    doc["sessions"][0].pop("project_source", None)
    path2.write_text(json.dumps(doc))
    got = read_snapshot(path2)
    assert got is not None and got.sessions[0].project_source == "live"


def test_bad_project_source_rejected(tmp_path: Path):
    path, doc = _doc(tmp_path)
    doc["sessions"][0]["project_source"] = "bogus"
    path.write_text(json.dumps(doc))
    assert read_snapshot(path) is None


def _strict_load(path: Path):
    def refuse(name):
        raise AssertionError(f"non-standard JSON constant {name}")

    return json.loads(path.read_bytes(), parse_constant=refuse)


def test_write_nonfinite_in_nested_dicts_roundtrips_as_null(tmp_path: Path):
    snap = make_snapshot()
    snap.host = {"a": float("nan"), "b": [float("inf"), {"c": float("-inf")}], "ok": 1.5}
    snap.quota = {"claude": {"5h": float("nan")}}
    snap.llm.gpu = {"util": float("inf")}
    path = tmp_path / "snap.json"
    write_snapshot(path, snap)
    doc = _strict_load(path)
    assert doc["host"] == {"a": None, "b": [None, {"c": None}], "ok": 1.5}
    back = read_snapshot(path)
    assert back is not None
    assert back.host == doc["host"]
    assert back.quota == {"claude": {"5h": None}}
    assert back.llm.gpu == {"util": None}


def test_write_nonfinite_in_stamped_field_writes_strict_json(tmp_path: Path):
    snap = make_snapshot()
    snap.sessions[0].project = Stamped("demo", float("nan"))
    snap.procs[0].cpu = float("inf")
    path = tmp_path / "snap.json"
    write_snapshot(path, snap)  # must not raise
    doc = _strict_load(path)
    assert doc["sessions"][0]["project"]["as_of"] is None
    assert doc["procs"][0]["cpu"] is None
    # INTENDED CHANGE (task76): a typed field that became null no longer costs the file; the
    # field reads as its default and the rest of the snapshot is intact.
    back = read_snapshot(path)
    assert back is not None
    assert back.sessions[0].project == Stamped("demo", 0.0)
    assert back.procs[0].cpu == 0.0 and back.procs[0].pid == 43


def test_write_deeply_nested_value_does_not_raise_and_reads_back(tmp_path: Path):
    deep: list = []
    cur = deep
    for _ in range(5000):
        nxt: list = []
        cur.append(nxt)
        cur = nxt
    snap = make_snapshot()
    snap.host = {"deep": deep}
    path = tmp_path / "snap.json"
    write_snapshot(path, snap)
    back = read_snapshot(path)
    assert back is not None
    depth = 0
    node = back.host["deep"]
    while node is not None:
        depth += 1
        node = node[0]
    assert 1 <= depth <= 32


def test_write_huge_structure_is_capped(tmp_path: Path):
    snap = make_snapshot()
    snap.quota = {"big": list(range(200_000))}
    path = tmp_path / "snap.json"
    write_snapshot(path, snap)
    doc = _strict_load(path)
    big = doc["quota"]["big"]
    assert 0 < len(big) < 100_000
    assert big[0] == 0 and None not in big
    assert read_snapshot(path) is not None


def test_write_logs_count_only_no_values(tmp_path: Path, caplog):
    snap = make_snapshot()
    snap.host = {"secret-key": float("nan"), "x": float("inf")}
    with caplog.at_level("WARNING", logger="fafo.store"):
        write_snapshot(tmp_path / "snap.json", snap)
    text = caplog.text
    assert "2" in text
    assert "secret-key" not in text and "nan" not in text.lower().replace("snapshot", "")


def test_write_nonfinite_keeps_mode_and_leaves_no_temp_files(tmp_path: Path):
    snap = make_snapshot()
    snap.host = {"a": float("nan")}
    path = tmp_path / "snap.json"
    write_snapshot(path, snap)
    assert stat.S_IMODE(path.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR
    assert [p.name for p in tmp_path.iterdir()] == ["snap.json"]


def test_write_huge_ints_become_null_and_boundary_kept(tmp_path: Path):
    snap = make_snapshot()
    snap.host = {
        "a": {"big": 10**5000, "neg": -(10**5000), "max": 2**63 - 1, "over": 2**63},
        "l": [10**5000],
    }
    path = tmp_path / "snap.json"
    write_snapshot(path, snap)  # must not raise
    back = read_snapshot(path)
    assert back is not None
    assert back.host["a"] == {"big": None, "neg": None, "max": 2**63 - 1, "over": None}
    assert back.host["l"] == [None]


def test_reader_refuses_wide_ints_consistently(tmp_path: Path):
    path = tmp_path / "snap.json"
    write_snapshot(path, make_snapshot())
    text = path.read_text().replace('"load":4.1', '"load":' + str(2**63))
    path.write_text(text)
    assert read_snapshot(path) is None
    path.write_text(text.replace(str(2**63), str(2**63 - 1)))
    assert read_snapshot(path) is not None


class _TripList(list):
    """List whose iteration raises if more than LIMIT elements are pulled."""

    LIMIT = 100_001

    def __iter__(self):
        for i in range(10**9):
            if i > self.LIMIT:
                raise AssertionError("iterated past the cap")
            yield i


class _TripDict(dict):
    def items(self):
        for i in range(10**9):
            if i > _TripList.LIMIT:
                raise AssertionError("iterated past the cap")
            yield f"k{i}", i

    def __iter__(self):
        raise AssertionError("dict iterated directly")


def test_write_caps_lazily_never_materializing_a_huge_container(tmp_path: Path):
    snap = make_snapshot()
    snap.quota = {"l": _TripList(), "d": _TripDict()}
    path = tmp_path / "snap.json"
    write_snapshot(path, snap)  # AssertionError here would mean unbounded iteration
    doc = _strict_load(path)
    n_l, n_d = len(doc["quota"]["l"]), len(doc["quota"]["d"])
    assert n_l + n_d < 100_000 and max(n_l, n_d) > 0


FLOAT_FIELDS = [
    ("taken", lambda d: d),
    ("as_of", lambda d: d["sessions"][0]["project"]),
    ("started", lambda d: d["sessions"][0]),
    ("cpu", lambda d: d["sessions"][0]),
    ("started", lambda d: d["procs"][0]),
    ("cpu", lambda d: d["procs"][0]),
    ("expires", lambda d: d["llm"]["models"][0]),
    ("started", lambda d: d["llm"]["inflight"][0]),
    ("elapsed", lambda d: d["runners"][0]),
    ("load", lambda d: d["runners"][0]),
    ("mem", lambda d: d["runners"][0]),
    ("disk", lambda d: d["runners"][0]),
    ("last_ok", lambda d: d["health"][0]),
]


def _good_doc(tmp_path: Path) -> dict:
    p = tmp_path / "good.json"
    write_snapshot(p, make_snapshot())
    return json.loads(p.read_text())


# INTENDED CHANGE (task76): this used to assert the whole file is refused; a bad typed float now
# reads as 0.0 and every other value survives (tests below pin that per field).
def test_huge_ints_in_every_typed_float_field_read_as_default(tmp_path: Path):
    assert len(FLOAT_FIELDS) >= 13
    path = tmp_path / "bad.json"
    for name, locate in FLOAT_FIELDS:
        for bad in (2**1024, 10**400, -(2**1024), None, float("nan"), float("inf"), "x"):
            doc = _good_doc(tmp_path)
            locate(doc)[name] = bad
            # the "llm" copy must change with llms[0], as the writer would have produced
            doc["llms"] = [doc["llm"]] if doc["llm"] is not None else []
            path.write_text(json.dumps(doc))
            snap = read_snapshot(path)
            assert snap is not None, (name, bad)
            _assert_well_typed(snap)
            assert snap.procs[0].pid == 43 and snap.runners[0].name == "r1", (name, bad)
            assert len(snap.sessions) == 1 and len(snap.health) == 1, (name, bad)
            if name == "taken":
                assert snap.taken == 0.0
            elif name == "as_of":
                assert snap.sessions[0].project == Stamped("demo", 0.0)


def test_writer_scrubs_huge_int_in_typed_float_field(tmp_path: Path):
    snap = make_snapshot()
    snap.procs[0].cpu = 10**400
    path = tmp_path / "snap.json"
    write_snapshot(path, snap)
    assert _strict_load(path)["procs"][0]["cpu"] is None
    back = read_snapshot(path)  # INTENDED CHANGE (task76): was refused; now the field is 0.0
    assert back is not None and back.procs[0].cpu == 0.0 and back.procs[0].pid == 43


def test_read_never_raises_on_seeded_mutations(tmp_path: Path):
    import random

    rng = random.Random(64)
    path = tmp_path / "fuzz.json"
    junk = [2**1024, 10**400, 2**63, True, False, -1, 1e308, "x", None, [], {}, "@@1e999@@"]

    def leaves(node, trail=()):
        if isinstance(node, dict):
            for k, v in node.items():
                yield from leaves(v, (*trail, k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from leaves(v, (*trail, i))
        else:
            yield trail

    for _ in range(300):
        doc = _good_doc(tmp_path)
        for _ in range(rng.randint(1, 3)):
            trail = rng.choice(list(leaves(doc)))
            if not trail:
                continue
            node = doc
            for k in trail[:-1]:
                node = node[k]
            node[trail[-1]] = rng.choice(junk)
        path.write_text(json.dumps(doc).replace('"@@1e999@@"', "1e999"))
        result = read_snapshot(path)  # must not raise
        assert result is None or isinstance(result, Snapshot)
        if result is not None:
            _assert_well_typed(result)


def test_sanitize_warning_wording_and_no_values(tmp_path: Path, caplog):
    snap = make_snapshot()
    snap.host = {"secret-key": float("nan"), 7: "dropped-key-value", "x": 10**400}
    with caplog.at_level("WARNING", logger="fafo.store"):
        write_snapshot(tmp_path / "snap.json", snap)
    msgs = [r.getMessage() for r in caplog.records if r.name == "fafo.store"]
    assert msgs == ["snapshot: sanitized 3 values (nulled, dropped or truncated)"]


def test_default_live_snapshot_has_the_pre_project_source_key_set(tmp_path: Path):
    path, doc = _doc(tmp_path)
    old = {f.name for f in fields(Session)} - {"project_source"}
    for s in doc["sessions"]:
        assert set(s) == old
    snap = make_snapshot()
    snap.sessions[0].project_source = "last"
    write_snapshot(path, snap)
    assert json.loads(path.read_text())["sessions"][0]["project_source"] == "last"
    got = read_snapshot(path)
    assert got is not None and got.sessions[0].project_source == "last"


# ---- bounded, validated snapshot open ----

def _written(tmp_path: Path) -> Path:
    p = tmp_path / "snap.json"
    write_snapshot(p, make_snapshot())
    return p


def _read_with_deadline(path: Path, seconds: float = 2.0):
    """Run the reader in a daemon thread so a regression fails instead of hanging."""
    out: list = []
    t = threading.Thread(target=lambda: out.append(read_snapshot(path)), daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "read_snapshot blocked"
    return out[0]


def test_normal_and_writer_mode_snapshot_still_reads(tmp_path):
    p = _written(tmp_path)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert read_snapshot(p) is not None


def test_fifo_snapshot_refused_without_blocking(tmp_path):
    p = tmp_path / "fifo.json"
    os.mkfifo(p)
    assert _read_with_deadline(p) is None


def test_symlink_snapshot_refused(tmp_path):
    real = _written(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert read_snapshot(real) is not None  # negative control: the target itself reads
    assert read_snapshot(link) is None


def test_directory_snapshot_refused(tmp_path):
    assert read_snapshot(tmp_path) is None


def test_group_or_other_readable_snapshot_refused(tmp_path):
    p = _written(tmp_path)
    for mode in (0o640, 0o604, 0o660, 0o606):
        p.chmod(mode)
        assert read_snapshot(p) is None, oct(mode)
    p.chmod(0o400)
    assert read_snapshot(p) is not None


def test_oversized_snapshot_refused_without_reading_it_all(tmp_path, monkeypatch):
    p = tmp_path / "big.json"
    with open(p, "wb") as f:
        f.truncate(store.MAX_SNAPSHOT_BYTES + 1)  # sparse
    p.chmod(0o600)
    real_read = os.read
    total = [0]

    def spy(fd, n):
        data = real_read(fd, n)
        total[0] += len(data)
        return data

    monkeypatch.setattr(store.os, "read", spy)
    assert read_snapshot(p) is None
    assert total[0] <= store.MAX_SNAPSHOT_BYTES


def test_growing_file_is_cut_off_at_the_cap(tmp_path, monkeypatch):
    """fstat says small, but the read stream is endless: the loop bound still refuses."""
    p = _written(tmp_path)
    monkeypatch.setattr(store, "MAX_SNAPSHOT_BYTES", 100)
    real_fstat = os.fstat

    class Fake:
        def __init__(self, st):
            self._st = st

        def __getattr__(self, name):
            return 10 if name == "st_size" else getattr(self._st, name)

    monkeypatch.setattr(store.os, "fstat", lambda fd: Fake(real_fstat(fd)))
    assert read_snapshot(p) is None


def test_wrong_owner_snapshot_refused(tmp_path, monkeypatch):
    p = _written(tmp_path)
    real_fstat = os.fstat

    class Fake:
        def __init__(self, st):
            self._st = st

        def __getattr__(self, name):
            return os.geteuid() + 1 if name == "st_uid" else getattr(self._st, name)

    monkeypatch.setattr(store.os, "fstat", lambda fd: Fake(real_fstat(fd)))
    assert read_snapshot(p) is None


# ---- per-row repair (task76): a bad typed value never costs the whole snapshot ----------------

def _assert_well_typed(snap: Snapshot) -> None:
    """Every row a reader returns is complete and right-typed: no partial row, no null number."""
    import math

    def num(v):
        return type(v) in (int, float) and math.isfinite(v)

    def count(v):
        return type(v) is int and 0 <= v < 2**63

    assert num(snap.taken)
    for s in snap.sessions:
        assert count(s.pid) and type(s.session_id) is str and num(s.started) and num(s.cpu)
        assert count(s.rss) and isinstance(s.project, Stamped) and num(s.project.as_of)
        assert type(s.project.value) is str
    for p in snap.procs:
        assert count(p.pid) and count(p.ppid) and num(p.cpu) and count(p.rss) and num(p.started)
        assert p.session_pid is None or count(p.session_pid)
    for srv in snap.llms:
        assert type(srv.name) is str and srv.name and count(srv.queue) and num(srv.last_ok)
        for m in srv.models:
            assert type(m.name) is str and count(m.vram) and count(m.context) and num(m.expires)
        for i in srv.inflight:
            assert type(i.model) is str and num(i.started)
            assert i.session_pid is None or count(i.session_pid)
    for r in snap.runners:
        assert type(r.name) is str and count(r.step_n) and count(r.step_total)
        assert num(r.elapsed) and num(r.load) and num(r.mem) and num(r.disk)
    for h in snap.health:
        assert num(h.last_ok)


BAD_NUMBERS = [None, float("nan"), float("inf"), float("-inf"), 2**1024, "x"]

# (container locator in the "doc", field, default after repair)
REPAIR_CASES = [
    ("session_started", lambda d: d["sessions"][0], "started", 0.0),
    ("session_cpu", lambda d: d["sessions"][0], "cpu", 0.0),
    ("session_rss", lambda d: d["sessions"][0], "rss", 0),
    ("proc_cpu", lambda d: d["procs"][0], "cpu", 0.0),
    ("proc_started", lambda d: d["procs"][0], "started", 0.0),
    ("proc_ppid", lambda d: d["procs"][0], "ppid", 0),
    ("proc_rss", lambda d: d["procs"][0], "rss", 0),
    ("proc_session_pid", lambda d: d["procs"][0], "session_pid", None),
    ("runner_elapsed", lambda d: d["runners"][0], "elapsed", 0.0),
    ("runner_load", lambda d: d["runners"][0], "load", 0.0),
    ("runner_mem", lambda d: d["runners"][0], "mem", 0.0),
    ("runner_disk", lambda d: d["runners"][0], "disk", 0.0),
    ("runner_step_n", lambda d: d["runners"][0], "step_n", 0),
    ("health_last_ok", lambda d: d["health"][0], "last_ok", 0.0),
]


def _write_doc(tmp_path: Path, doc: dict) -> Path:
    doc["llm"] = doc["llms"][0] if doc["llms"] else None  # the writer keeps these in step
    path = tmp_path / "edited.json"
    path.write_text(json.dumps(doc))
    return path


@pytest.mark.parametrize("bad", BAD_NUMBERS, ids=repr)
@pytest.mark.parametrize("case", REPAIR_CASES, ids=lambda c: c[0])
def test_bad_number_in_one_field_reads_as_default_and_rows_stay(tmp_path: Path, case, bad):
    _, locate, field_name, default = case
    doc = _good_doc(tmp_path)
    locate(doc)[field_name] = bad
    snap = read_snapshot(_write_doc(tmp_path, doc))
    assert snap is not None
    expect = make_snapshot()
    got_row = {
        "session": snap.sessions, "proc": snap.procs, "runner": snap.runners,
        "health": snap.health,
    }[case[0].split("_")[0]]
    assert len(got_row) == 1
    assert getattr(got_row[0], field_name) == default
    # everything else is untouched
    assert snap.host == expect.host and snap.quota == expect.quota and snap.llms == expect.llms
    _assert_well_typed(snap)


def test_bad_llm_numbers_repair_in_llms_and_the_llm_copy(tmp_path: Path):
    for bad in BAD_NUMBERS:
        for where, field_name, default in (
            (lambda s: s, "queue", 0), (lambda s: s, "last_ok", 0.0),
            (lambda s: s["models"][0], "vram", 0), (lambda s: s["models"][0], "context", 0),
            (lambda s: s["models"][0], "expires", 0.0),
            (lambda s: s["inflight"][0], "started", 0.0),
            (lambda s: s["inflight"][0], "session_pid", None),
        ):
            doc = _good_doc(tmp_path)
            where(doc["llms"][0])[field_name] = bad
            snap = read_snapshot(_write_doc(tmp_path, doc))
            assert snap is not None and len(snap.llms) == 1, (field_name, bad)
            srv = snap.llms[0]
            target = {"queue": srv, "last_ok": srv}.get(field_name)
            if target is None:
                target = srv.models[0] if field_name in ("vram", "context", "expires") \
                    else srv.inflight[0]
            assert getattr(target, field_name) == default, (field_name, bad)
            _assert_well_typed(snap)


def test_stamped_as_of_bad_reads_zero_so_the_session_is_very_old_not_hidden(tmp_path: Path):
    for bad in BAD_NUMBERS:
        doc = _good_doc(tmp_path)
        doc["sessions"][0]["project"]["as_of"] = bad
        snap = read_snapshot(_write_doc(tmp_path, doc))
        assert snap is not None and len(snap.sessions) == 1
        assert snap.sessions[0].project == Stamped("demo", 0.0)
        assert snap.taken - snap.sessions[0].project.as_of > 900  # ages out under the age rule


def test_nan_and_infinity_tokens_in_typed_fields_are_repaired_not_refused(tmp_path: Path):
    path = tmp_path / "snap.json"
    write_snapshot(path, make_snapshot())
    text = path.read_text()
    assert '"cpu":0.1' in text
    path.write_text(text.replace('"cpu":0.1', '"cpu":NaN').replace('"mem":0.5', '"mem":-Infinity'))
    snap = read_snapshot(path)
    assert snap is not None and snap.procs[0].cpu == 0.0 and snap.runners[0].mem == 0.0


def test_bad_pid_drops_only_that_row(tmp_path: Path):
    for bad in (None, "7", -1, 2**1024, True, float("nan")):
        doc = _good_doc(tmp_path)
        second = json.loads(json.dumps(doc["procs"][0]))
        second["pid"] = 99
        doc["procs"] = [{**doc["procs"][0], "pid": bad}, second]
        sess2 = json.loads(json.dumps(doc["sessions"][0]))
        sess2["pid"], sess2["session_id"] = 77, "other"
        doc["sessions"] = [{**doc["sessions"][0], "pid": bad}, sess2]
        snap = read_snapshot(_write_doc(tmp_path, doc))
        assert snap is not None, bad
        assert [p.pid for p in snap.procs] == [99], bad
        assert [s.pid for s in snap.sessions] == [77], bad
        assert len(snap.runners) == 1 and len(snap.llms) == 1 and len(snap.health) == 1
        _assert_well_typed(snap)


def test_bad_session_id_runner_name_and_server_name_drop_only_that_row(tmp_path: Path):
    doc = _good_doc(tmp_path)
    good_r = doc["runners"][0]
    doc["runners"] = [{**good_r, "name": None}, {**good_r, "name": "r2"}]
    s0 = doc["sessions"][0]
    doc["sessions"] = [{**s0, "session_id": None}, {**s0, "session_id": "ok", "pid": 5}]
    l0 = doc["llms"][0]
    doc["llms"] = [{**l0, "name": 7}, {**l0, "name": "beta"}]
    doc["llm"] = doc["llms"][0]  # the writer copies the first; it is bad too
    path = tmp_path / "e.json"
    path.write_text(json.dumps(doc))
    snap = read_snapshot(path)
    assert snap is not None
    assert [r.name for r in snap.runners] == ["r2"]
    assert [s.session_id for s in snap.sessions] == ["ok"]
    assert [s.name for s in snap.llms] == ["beta"]


def test_structural_damage_still_refuses_the_whole_file(tmp_path: Path):
    path = tmp_path / "snap.json"
    for mutate in (
        lambda d: d.pop("sessions"),
        lambda d: d.update(extra=1),
        lambda d: d.update(version=9),
        lambda d: d.update(sessions={}),
        lambda d: d["sessions"][0].update(agent=None),  # a bad string is not a number: refuse
        lambda d: d["sessions"][0].pop("cpu"),
        lambda d: d["procs"][0].update(unknown=1),
        lambda d: d.update(host={"x": 2**1024}),
    ):
        doc = _good_doc(tmp_path)
        mutate(doc)
        path.write_text(json.dumps(doc))
        assert read_snapshot(path) is None
    path.write_text("[" * 5000 + "]" * 5000)
    assert read_snapshot(path) is None
    path.write_bytes(b"x" * 10)
    assert read_snapshot(path) is None


def test_repair_logs_counts_only_never_values(tmp_path: Path, caplog):
    doc = _good_doc(tmp_path)
    doc["procs"][0]["cpu"] = "SECRET-VALUE-xyz"
    doc["runners"][0]["load"] = None
    doc["sessions"][0]["pid"] = "SECRET-PID-abc"
    path = _write_doc(tmp_path, doc)
    with caplog.at_level("DEBUG", logger="fafo.store"):
        assert read_snapshot(path) is not None
    msgs = [r.getMessage() for r in caplog.records if r.name == "fafo.store"]
    assert msgs == ["snapshot: repaired 2 values, dropped 1 rows"]
    assert "SECRET" not in caplog.text


def test_clean_file_logs_nothing(tmp_path: Path, caplog):
    path, _ = _doc(tmp_path)
    with caplog.at_level("DEBUG", logger="fafo.store"):
        assert read_snapshot(path) is not None
    assert [r for r in caplog.records if r.name == "fafo.store"] == []


def test_fuzz_300_seeded_mutations_never_raise_and_rows_are_whole(tmp_path: Path):
    import random

    rng = random.Random(76)
    path = tmp_path / "fuzz.json"
    junk = [None, float("nan"), float("inf"), 2**1024, 10**400, -1, True, "x", 1.5, [], {}]
    base = _good_doc(tmp_path)
    base["llm"] = base["llms"][0]

    def leaves(node, trail=()):
        if isinstance(node, dict):
            for k, v in node.items():
                yield from leaves(v, (*trail, k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from leaves(v, (*trail, i))
        else:
            yield trail

    all_leaves = [t for t in leaves(base) if t]
    assert len(all_leaves) > 60
    kept = 0
    for _ in range(300):
        doc = json.loads(json.dumps(base))
        for _ in range(rng.randint(1, 4)):
            trail = rng.choice(all_leaves)
            node = doc
            for k in trail[:-1]:
                node = node[k]
            node[trail[-1]] = rng.choice(junk)
        if rng.random() < 0.5 and doc["llms"]:
            doc["llm"] = doc["llms"][0]  # half the cases keep the copy in step
        path.write_text(json.dumps(doc))
        snap = read_snapshot(path)  # must not raise
        assert snap is None or isinstance(snap, Snapshot)
        if snap is not None:
            kept += 1
            _assert_well_typed(snap)
    assert kept > 50  # repair really keeps files; this is not a vacuous all-None run


def test_two_servers_bad_second_name_drops_it_and_bad_last_ok_repairs(tmp_path: Path):
    path = tmp_path / "snap.json"
    write_snapshot(path, two_servers())
    doc = json.loads(path.read_bytes())
    doc["llms"][1]["name"] = 7
    path.write_text(json.dumps(doc))
    snap = read_snapshot(path)
    assert snap is not None and [s.name for s in snap.llms] == ["alpha"]
    doc["llms"][1]["name"] = "beta"
    doc["llms"][1]["last_ok"] = float("nan")
    path.write_text(json.dumps(doc))
    snap = read_snapshot(path)
    assert snap is not None and [s.last_ok for s in snap.llms] == [990.0, 0.0]


# ---- round 2: identity is decided only after the whole row validated ---------------------------

def test_bad_identity_alone_drops_only_that_row(tmp_path: Path):
    doc = _good_doc(tmp_path)
    second = {**doc["procs"][0], "pid": 99}
    doc["procs"] = [{**doc["procs"][0], "pid": None}, second]
    snap = read_snapshot(_write_doc(tmp_path, doc))
    assert snap is not None and [p.pid for p in snap.procs] == [99]


def test_bad_pid_with_structural_damage_in_the_same_row_refuses_the_file(tmp_path: Path):
    for damage in ({"cmd": None}, {"agent": 5}):
        doc = _good_doc(tmp_path)
        doc["procs"][0].update(pid=None, **damage)
        assert read_snapshot(_write_doc(tmp_path, doc)) is None, damage
    doc = _good_doc(tmp_path)
    doc["sessions"][0].update(pid="x", agent=None)
    assert read_snapshot(_write_doc(tmp_path, doc)) is None
    doc = _good_doc(tmp_path)
    doc["sessions"][0].update(session_id=None, project="not-a-dict")
    assert read_snapshot(_write_doc(tmp_path, doc)) is None


def test_bad_server_name_with_structural_damage_in_the_same_row_refuses_the_file(tmp_path: Path):
    for damage in ({"models": {}}, {"inflight": "x"}, {"gpu": []}, {"models": [{"name": 1}]}):
        doc = _good_doc(tmp_path)
        doc["llms"][0].update(name=7, **damage)
        assert read_snapshot(_write_doc(tmp_path, doc)) is None, damage


def test_missing_name_key_is_structural_and_a_non_string_name_is_a_row_drop(tmp_path: Path):
    # A missing "name" key is a key-set mismatch: structural, the file is refused (same case as
    # the pop("name") entry in test_bad_llms_rejected). A present but non-string name is a bad
    # identity VALUE: only that server is dropped.
    path = tmp_path / "snap.json"
    write_snapshot(path, two_servers())
    doc = json.loads(path.read_bytes())
    del doc["llms"][1]["name"]
    path.write_text(json.dumps(doc))
    assert read_snapshot(path) is None
    doc["llms"][1]["name"] = 7
    path.write_text(json.dumps(doc))
    snap = read_snapshot(path)
    assert snap is not None and [s.name for s in snap.llms] == ["alpha"]


@pytest.mark.parametrize("bad", [None, 7, "x" * 500, ["a"], True])
def test_bad_health_name_drops_only_that_health_row(tmp_path: Path, bad):
    doc = _good_doc(tmp_path)
    doc["health"] = [{**doc["health"][0], "name": bad}, {**doc["health"][0], "name": "ok"}]
    snap = read_snapshot(_write_doc(tmp_path, doc))
    assert snap is not None, bad
    assert [h.name for h in snap.health] == ["ok"]
    assert len(snap.sessions) == 1 and len(snap.procs) == 1


def test_bad_health_name_with_structural_damage_refuses(tmp_path: Path):
    doc = _good_doc(tmp_path)
    doc["health"][0].update(name=None, status="broken")
    assert read_snapshot(_write_doc(tmp_path, doc)) is None


def test_unexpected_exception_in_the_row_validator_returns_none_and_logs_class_only(
    tmp_path: Path, monkeypatch, caplog
):
    from fafo import store

    def boom(v):
        raise RuntimeError("SECRET-detail")

    path, _ = _doc(tmp_path)
    monkeypatch.setitem(store._SPEC[Proc], "cmd", boom)
    with caplog.at_level("DEBUG", logger="fafo.store"):
        assert read_snapshot(path) is None  # must not raise
    assert "RuntimeError" in caplog.text and "SECRET" not in caplog.text
