import logging

from sources_test_codex import FakeProc

from fafo.config import SourceCfg
from fafo.source import Ctx
from fafo.sources import gemini
from fafo.sources.gemini import GeminiSource


def ctx():
    return Ctx(cfg=SourceCfg(), clock=lambda: 0.0, log=logging.getLogger("t"))


def run(procs):
    return GeminiSource(lister=lambda: list(procs), show_args={"--model"}).collect(ctx())


def test_capability_and_name():
    assert gemini.CAPABILITY == "process-only"
    assert GeminiSource(lister=list, show_args=set()).name == "gemini"


def test_recognized_by_basename_and_by_node_script():
    procs = [
        FakeProc(10, 1, ["/usr/local/bin/gemini", "--model", "m"]),
        FakeProc(11, 1, ["node", "/usr/lib/node_modules/@g/cli/bin/gemini", "x"]),
        FakeProc(12, 1, ["node", "/srv/app/server.js"]),
        FakeProc(13, 1, ["bash"]),
    ]
    res = run(procs)
    assert res.status == "ok"
    assert [p.pid for p in res.procs] == [10, 11]
    assert all(p.agent == "gemini" for p in res.procs)
    assert res.sessions == []  # never invents task or session detail


def test_claude_ancestor_and_orphan():
    procs = [
        FakeProc(20, 1, ["claude"], started=1.0),
        FakeProc(21, 20, ["bash"], started=2.0),
        FakeProc(22, 21, ["gemini"], started=3.0),
        FakeProc(23, 1, ["gemini"], started=3.0),
    ]
    by = {p.pid: p for p in run(procs).procs}
    assert by[22].session_pid == 20
    assert by[23].session_pid is None


def test_vanished_skipped_and_empty_ok():
    res = run([FakeProc(30, 1, ["gemini"], vanish=True), FakeProc(31, 1, ["gemini"])])
    assert [p.pid for p in res.procs] == [31]
    empty = run([])
    assert empty.status == "ok" and empty.procs == []


def test_redaction_and_cap():
    secret = "ghp_" + "b" * 36
    res = run([FakeProc(40, 1, ["gemini", "--token=zzz999", secret])])
    assert "zzz999" not in res.procs[0].cmd and secret not in res.procs[0].cmd
    many = [FakeProc(1000 + i, 1, ["gemini"]) for i in range(200)]
    assert len(run(many).procs) == 128


def test_codex_not_matched():
    assert run([FakeProc(50, 1, ["codex", "exec"])]).procs == []
