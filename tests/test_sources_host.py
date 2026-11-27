"""Tests for the host source. All psutil access goes through a stub Probe; nvidia-smi is a
real child process only where the test is about the deadline (a script in a tmp dir)."""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
from collections import namedtuple
from pathlib import Path

from conftest import Ran, deadline_bound, run_when_ready, wait_gone

from fafo.config import SourceCfg
from fafo.source import Ctx
from fafo.sources.host import MAX_ITEMS, HostSource

Mem = namedtuple("Mem", "total used percent")
Swap = namedtuple("Swap", "total used")
Part = namedtuple("Part", "device mountpoint fstype")
Usage = namedtuple("Usage", "total used percent")
Net = namedtuple("Net", "bytes_recv bytes_sent")

NO_GPU = "/nonexistent/fafo-test-no-nvidia-smi"


class StubProbe:
    def __init__(self):
        self.fail: set[str] = set()
        self.parts = [Part("/dev/sda1", "/", "ext4"), Part("/dev/sda2", "/boot", "vfat")]
        self.net = {"eth0": Net(1000, 2000)}

    def _chk(self, name):
        if name in self.fail:
            raise OSError(name)

    def load_avg(self):
        self._chk("load")
        return (0.5, 1.5, 2.5)

    def cpu_percent(self):
        self._chk("cpu_percent")
        return 12.5

    def cpu_count(self):
        self._chk("cpu_count")
        return 8

    def virtual_memory(self):
        self._chk("mem")
        return Mem(1000, 400, 40.0)

    def swap_memory(self):
        self._chk("swap")
        return Swap(500, 50)

    def disk_partitions(self):
        self._chk("disks")
        return list(self.parts)

    def disk_usage(self, mount):
        return Usage(100, 25, 25.0)

    def net_io_counters(self):
        self._chk("net")
        return dict(self.net)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make_ctx(clock=None):
    return Ctx(cfg=SourceCfg(), clock=clock or Clock(), log=logging.getLogger("t"))


def make_src(probe=None, **kw):
    kw.setdefault("nvidia_smi", NO_GPU)
    return HostSource(probe=probe or StubProbe(), **kw)


def write_script(tmp_path: Path, body: str, name: str = "nvidia-smi") -> str:
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(stat.S_IRWXU)
    return str(p)


def test_normal_values_mapped():
    res = make_src().collect(make_ctx())
    h = res.host
    assert res.status is None or res.status == "ok"
    assert (h["load1"], h["load5"], h["load15"]) == (0.5, 1.5, 2.5)
    assert h["cpu_percent"] == 12.5 and h["cpu_count"] == 8
    assert (h["mem_total"], h["mem_used"], h["mem_percent"]) == (1000, 400, 40.0)
    assert (h["swap_total"], h["swap_used"]) == (500, 50)
    assert {d["mount"] for d in h["disks"]} == {"/", "/boot"}
    assert h["disks"][0] == {"mount": "/", "total": 100, "used": 25, "percent": 25.0}
    json.dumps(h)  # JSON safe


def test_no_nvidia_smi_gives_no_gpus_and_status_ok():
    res = make_src().collect(make_ctx())
    assert "gpus" not in res.host
    assert res.status in (None, "ok")
    assert res.host["load1"] == 0.5


def test_gpus_parsed(tmp_path):
    exe = write_script(
        tmp_path, "echo '0, NVIDIA Test, GPU, 37, 1024, 8192'\necho '1, Other, 5, 10, 20'"
    )
    res = make_src(nvidia_smi=exe).collect(make_ctx())
    gpus = res.host["gpus"]
    assert gpus[0] == {
        "index": 0, "name": "NVIDIA Test, GPU", "util": 37, "mem_used": 1024, "mem_total": 8192
    }
    assert gpus[1]["index"] == 1 and gpus[1]["name"] == "Other"


def test_garbage_nvidia_output_ignored(tmp_path):
    exe = write_script(tmp_path, "echo 'not csv'\necho 'a, b, c, d, e'\necho '0, n, [N/A], 1, 2'\n"
                       "printf '\\377\\376\\n'")
    res = make_src(nvidia_smi=exe).collect(make_ctx())
    assert not res.host.get("gpus")
    assert res.status in (None, "ok")
    assert res.host["load1"] == 0.5


def test_oversize_nvidia_output_ignored(tmp_path):
    exe = write_script(tmp_path, "yes '0, n, 1, 2, 3' | head -c 200000")
    res = make_src(nvidia_smi=exe).collect(make_ctx())
    assert not res.host.get("gpus")


def test_nvidia_failure_exit_ignored(tmp_path):
    exe = write_script(tmp_path, "echo '0, n, 1, 2, 3'\nexit 9")
    res = make_src(nvidia_smi=exe).collect(make_ctx())
    assert not res.host.get("gpus")


def test_nvidia_hang_cut_by_deadline_group_killed(tmp_path):
    def scenario(deadline):
        pidfile = tmp_path / f"pid-{deadline}"
        exe = write_script(
            tmp_path, f"sh -c 'echo $$ > {pidfile}; exec sleep 30' &\nwait", f"smi-{deadline}"
        )
        src = make_src(nvidia_smi=exe, gpu_timeout_s=deadline)
        box: dict = {}

        def run():
            box["res"] = src.collect(make_ctx())

        t0 = time.monotonic()
        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(deadline + 12)  # guard: a missing deadline fails here instead of hanging
        box["alive"] = th.is_alive()
        box["elapsed"] = time.monotonic() - t0
        return Ran(box, pidfile)

    r = run_when_ready(scenario)
    box = r.result
    try:
        assert not box["alive"], "collect hung past the deadline"
        assert box["elapsed"] < deadline_bound(r.deadline)
        assert "gpus" not in box["res"].host
        assert box["res"].host["load1"] == 0.5
        assert wait_gone(r.pids), "child survived the deadline"
    finally:
        for pid in r.pids:  # only a test failure leaves it; kill the exact pid we recorded
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass


def test_nvidia_deadline_is_accurate(tmp_path):
    """The one tight timing test: a trivial child, deadline <= elapsed < deadline + slack."""
    exe = write_script(tmp_path, "exec sleep 30")
    src = make_src(nvidia_smi=exe, gpu_timeout_s=1.0)
    t0 = time.monotonic()
    res = src.collect(make_ctx())
    assert 1.0 <= time.monotonic() - t0 < deadline_bound(1.0)
    assert "gpus" not in res.host


def test_one_metric_failing_drops_only_that_metric():
    p = StubProbe()
    p.fail = {"mem"}
    h = make_src(p).collect(make_ctx()).host
    assert "mem_total" not in h and "mem_percent" not in h
    assert h["load1"] == 0.5 and h["swap_total"] == 500 and h["disks"]


def test_all_psutil_failing_is_absent():
    p = StubProbe()
    p.fail = {"load", "cpu_percent", "cpu_count", "mem", "swap", "disks", "net"}
    res = make_src(p).collect(make_ctx())
    assert res.status == "absent"


def test_psutil_import_failure_is_absent():
    def boom():
        raise ImportError("no psutil")

    res = HostSource(probe_factory=boom, nvidia_smi=NO_GPU).collect(make_ctx())
    assert res.status == "absent"


def test_net_rates_from_two_calls():
    clock = Clock()
    p = StubProbe()
    src = make_src(p)
    first = src.collect(make_ctx(clock)).host["net"]
    assert first == [{"iface": "eth0", "rx_bps": 0, "tx_bps": 0}]
    clock.t += 2.0
    p.net = {"eth0": Net(1000 + 4000, 2000 + 1000)}
    second = src.collect(make_ctx(clock)).host["net"]
    assert second == [{"iface": "eth0", "rx_bps": 2000.0, "tx_bps": 500.0}]
    clock.t += 2.0
    p.net = {"eth0": Net(10, 10)}  # counter reset: never negative
    third = src.collect(make_ctx(clock)).host["net"]
    assert third[0]["rx_bps"] == 0 and third[0]["tx_bps"] == 0


def test_pseudo_filesystems_skipped():
    p = StubProbe()
    p.parts = [
        Part("proc", "/proc", "proc"),
        Part("sysfs", "/sys", "sysfs"),
        Part("tmpfs", "/run", "tmpfs"),
        Part("cgroup2", "/sys/fs/cgroup", "cgroup2"),
        Part("/dev/loop1", "/snap/x/1", "squashfs"),
        Part("overlay", "/var/lib/docker/overlay2/a", "overlay"),
        Part("none", "/weird", "autofs"),
        Part("/dev/sda1", "/", "ext4"),
    ]
    h = make_src(p).collect(make_ctx()).host
    assert [d["mount"] for d in h["disks"]] == ["/"]


def test_caps_at_16():
    p = StubProbe()
    p.parts = [Part(f"/dev/sd{i}", f"/m{i}", "ext4") for i in range(40)]
    p.net = {f"if{i}": Net(i, i) for i in range(40)}
    h = make_src(p).collect(make_ctx()).host
    assert MAX_ITEMS == 16
    assert len(h["disks"]) == 16 and len(h["net"]) == 16


def test_strings_sanitized_and_no_identity_fields():
    p = StubProbe()
    esc = chr(27)
    p.parts = [Part("/dev/sda1", "/mnt/a" + esc + "]0;x" + chr(7) + "b", "ext4")]
    p.net = {"et" + esc + "h0": Net(1, 1)}
    h = make_src(p).collect(make_ctx()).host
    assert esc not in json.dumps(h) and chr(7) not in json.dumps(h)
    assert not {"hostname", "user", "ip", "mac"} & set(h)


def test_normal_exit_kills_group_once_before_reap(tmp_path, group_kill_spy):
    from fafo.sources.host import _MIN_ENV, _run_capped

    exe = write_script(tmp_path, "echo '0, n, 1, 2, 3'")
    assert b"0, n" in _run_capped([exe], 10.0, _MIN_ENV)
    kills = group_kill_spy.kills()
    assert len(kills) == 1
    assert group_kill_spy.events[0] == kills[0]  # no wait before the kill
    assert group_kill_spy.reaped_when_killed == []


def test_normal_exit_leaves_no_grandchild(tmp_path):
    from fafo.sources.host import _MIN_ENV, _run_capped

    def scenario(deadline):
        pidfile = tmp_path / f"gpid-{deadline}"
        exe = write_script(
            tmp_path,
            f"sleep 30 >/dev/null 2>&1 &\necho $! > {pidfile}\necho '0, n, 1, 2, 3'",
            f"smi-{deadline}",
        )
        return Ran(_run_capped([exe], max(deadline, 10.0), _MIN_ENV), pidfile)

    r = run_when_ready(scenario)
    assert b"0, n" in r.result
    if wait_gone(r.pids):
        return
    for pid in r.pids:
        os.kill(pid, 9)
    raise AssertionError("grandchild survived a normal exit")
