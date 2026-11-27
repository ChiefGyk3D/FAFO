import json
import stat
import threading
from pathlib import Path

from fafo.model import (
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
        version=1,
        taken=taken,
        host={"load": 4.1},
        sessions=[session],
        procs=[proc],
        llm=llm,
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
    snap = Snapshot(**{**snap.__dict__, "llm": None})
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
    doc["version"] = 2
    path.write_text(json.dumps(doc))
    assert read_snapshot(path) is None
    doc["version"] = 1
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
