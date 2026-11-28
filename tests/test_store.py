import json
import stat
import threading
from dataclasses import fields
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
from fafo.store import read_snapshot, write_snapshot


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


def test_wrong_types_return_none(tmp_path: Path):
    cases = _mutations()
    assert len(cases) > 30
    for name, apply in cases.items():
        path, doc = _doc(tmp_path)
        apply(doc)
        # json.dumps emits NaN/Infinity tokens, which json.loads accepts: the validator must reject
        path.write_text(json.dumps(doc))
        assert read_snapshot(path) is None, name


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
        lambda d: d["llms"][1].update(name=7),
        lambda d: d["llms"][1].update(name="x" * 101),
        lambda d: d.update(llm=None),
        lambda d: d["llms"][1].update(reachable="yes"),
        lambda d: d["llms"][1].update(last_ok=float("nan")),
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
    # a typed field that became null is refused by the reader, cleanly
    assert read_snapshot(path) is None


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


def test_huge_ints_in_every_typed_float_field_refuse_cleanly(tmp_path: Path):
    path = tmp_path / "bad.json"
    for name, locate in FLOAT_FIELDS:
        for bad in (2**1024, 10**400, -(2**1024)):
            doc = _good_doc(tmp_path)
            locate(doc)[name] = bad
            path.write_text(json.dumps(doc))
            assert read_snapshot(path) is None, (name, bad)


def test_writer_scrubs_huge_int_in_typed_float_field(tmp_path: Path):
    snap = make_snapshot()
    snap.procs[0].cpu = 10**400
    path = tmp_path / "snap.json"
    write_snapshot(path, snap)
    assert _strict_load(path)["procs"][0]["cpu"] is None
    assert read_snapshot(path) is None  # typed field became null: refused, not raised


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
