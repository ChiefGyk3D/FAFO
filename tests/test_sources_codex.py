import logging

import psutil
import pytest

from fafo.config import SourceCfg
from fafo.source import Ctx
from fafo.sources.codex import MAX_ROWS, CodexSource

SHOW = {"exec", "resume", "--resume", "--model"}


class _Mem:
    def __init__(self, rss):
        self.rss = rss


class FakeProc:
    """Stub with the psutil.Process methods the source uses."""

    def __init__(self, pid, ppid, argv, cpu=1.5, rss=4096, started=100.0, vanish=False):
        self.pid = pid
        self._ppid = ppid
        self._argv = argv
        self._cpu = cpu
        self._rss = rss
        self._started = started
        self._vanish = vanish

    def _check(self):
        if self._vanish:
            raise psutil.NoSuchProcess(self.pid)

    def ppid(self):
        return self._ppid

    def cmdline(self):
        self._check()
        return list(self._argv)

    def cpu_percent(self, interval=None):
        self._check()
        return self._cpu

    def memory_info(self):
        self._check()
        return _Mem(self._rss)

    def create_time(self):
        self._check()
        return self._started


def ctx():
    return Ctx(cfg=SourceCfg(), clock=lambda: 0.0, log=logging.getLogger("t"))


def run(procs, show=SHOW):
    return CodexSource(lister=lambda: list(procs), show_args=show).collect(ctx())


CLAUDE = ["claude", "--resume", "abc"]


@pytest.mark.parametrize(
    "argv",
    [
        ["/usr/bin/codex", "exec", "--model", "gpt-x", "fix it"],
        ["/usr/lib/codex/codex-code-mode-host", "--socket", "/run/x"],
        ["codex", "app-server", "--listen", "stdio"],
        ["codex", "app-server", "daemon", "run"],
        ["/opt/Codex/resources/codex", "--flag", "exec-server", "--port", "1"],
        ["node", "/usr/lib/node_modules/@x/codex/bin/codex", "exec", "go"],
    ],
)
def test_codex_command_shapes_recognized(argv):
    res = run([FakeProc(10, 1, argv), FakeProc(11, 1, ["bash"])])
    assert res.status == "ok"
    assert [p.pid for p in res.procs] == [10]
    p = res.procs[0]
    assert (p.agent, p.ppid, p.session_pid) == ("codex", 1, None)
    assert (p.cpu, p.rss, p.started) == (1.5, 4096, 100.0)


def test_claude_ancestor_found_across_levels_and_gap():
    procs = [
        FakeProc(1, 0, ["init"], started=1.0),
        FakeProc(20, 1, CLAUDE, started=10.0),
        FakeProc(21, 20, ["node", "/x/helper.js"], started=11.0),
        FakeProc(22, 21, ["bash", "-c", "run"], started=12.0),  # gap: a shell
        FakeProc(23, 22, ["codex", "exec", "task"], started=13.0),
        FakeProc(24, 20, ["codex", "exec", "direct"], started=14.0),
    ]
    res = run(procs)
    by = {p.pid: p for p in res.procs}
    assert by[23].session_pid == 20
    assert by[24].session_pid == 20


def test_nearest_claude_ancestor_wins():
    procs = [
        FakeProc(30, 1, CLAUDE, started=1.0),
        FakeProc(31, 30, ["claude"], started=2.0),
        FakeProc(32, 31, ["codex", "exec", "x"], started=3.0),
    ]
    assert run(procs).procs[0].session_pid == 31


def test_orphan_codex_has_no_session_not_a_crash():
    res = run(
        [FakeProc(40, 1, ["codex", "exec", "x"]), FakeProc(41, 99999, ["codex-code-mode-host"])]
    )
    assert res.status == "ok"
    assert sorted(p.pid for p in res.procs) == [40, 41]
    assert all(p.session_pid is None for p in res.procs)


def test_ancestor_cycle_terminates():
    procs = [FakeProc(50, 51, ["bash"]), FakeProc(51, 50, ["codex", "exec"])]
    assert run(procs).procs[0].session_pid is None


def test_reused_pid_ancestor_started_later_is_not_owner():
    # A "claude" that started after the codex process cannot be its parent.
    procs = [
        FakeProc(60, 1, CLAUDE, started=500.0),
        FakeProc(61, 60, ["codex", "exec", "x"], started=100.0),
    ]
    assert run(procs).procs[0].session_pid is None


def test_vanished_process_is_skipped():
    procs = [
        FakeProc(70, 1, ["codex", "exec", "gone"], vanish=True),
        FakeProc(71, 1, ["codex", "exec", "here"]),
        FakeProc(72, 1, ["bash"], vanish=True),
    ]
    res = run(procs)
    assert [p.pid for p in res.procs] == [71]
    assert res.status == "ok"


def test_vanished_claude_ancestor_means_no_owner():
    procs = [
        FakeProc(80, 1, CLAUDE, vanish=True, started=1.0),
        FakeProc(81, 80, ["codex", "exec", "x"], started=2.0),
    ]
    assert run(procs).procs[0].session_pid is None


def test_token_shaped_argument_is_redacted():
    secret = "ghp_" + "a" * 36
    argv = ["codex", "exec", "--token=abc123SECRET", secret, "--model", "gpt-x"]
    cmd = run([FakeProc(90, 1, argv)]).procs[0].cmd
    assert "abc123SECRET" not in cmd
    assert secret not in cmd
    assert cmd.startswith("codex exec")
    assert "--model gpt-x" in cmd


def test_control_chars_in_cmd_stripped():
    esc = chr(27)
    bel = chr(7)
    res = run([FakeProc(91, 1, ["codex", "exec", esc + "]0;pwn" + bel])], show={"exec"})
    assert esc not in res.procs[0].cmd and bel not in res.procs[0].cmd


def test_unknown_processes_ignored():
    procs = [
        FakeProc(1, 0, ["init"]),
        FakeProc(2, 1, ["python", "codex_notes.py"]),
        FakeProc(3, 1, ["vim", "codex"]),
        FakeProc(4, 1, []),
    ]
    res = run(procs)
    assert res.procs == [] and res.status == "ok"


def test_row_cap_128():
    procs = [FakeProc(1000 + i, 1, ["codex", "exec", str(i)]) for i in range(300)]
    res = run(procs)
    assert MAX_ROWS == 128
    assert len(res.procs) == 128


def test_empty_table_is_ok_not_absent():
    res = run([])
    assert res.status == "ok"
    assert res.procs == [] and res.sessions == []


def test_source_name():
    assert CodexSource(lister=list, show_args=set()).name == "codex"
