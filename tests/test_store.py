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
