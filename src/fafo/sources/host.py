"""Host source: load, CPU, memory, swap, disks, network rates and NVIDIA GPUs.

All psutil access goes through one small Probe so tests inject a stub. The output holds only
numbers and short sanitized strings: no hostname, user name, address or MAC. A metric that
raises is dropped alone. The status is "absent" only when psutil itself is unusable (import
fails or every metric fails). nvidia-smi is optional: when missing, `gpus` is left out and the
source stays ok. It runs as an argument list with a hard deadline for the whole call, a
minimal environment, stdin closed and output capped at 64 KB; the process group is killed on
deadline or oversize.
"""

from __future__ import annotations

import math
import os
import selectors
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from fafo.redact import sanitize_text
from fafo.source import Ctx, SourceResult

MAX_ITEMS = 16
MAX_GPU_OUTPUT = 64 * 1024
GPU_TIMEOUT_S = 2.0
_STR_LEN = 100
_GPU_ARGS = (
    "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
    "--format=csv,noheader,nounits",
)
_MIN_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}

# Filesystems that are not real local storage. Anything else that psutil lists is kept.
_PSEUDO_FSTYPES = frozenset(
    {
        "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "ramfs", "cgroup", "cgroup2", "pstore",
        "bpf", "debugfs", "tracefs", "securityfs", "configfs", "fusectl", "mqueue", "hugetlbfs",
        "autofs", "binfmt_misc", "overlay", "squashfs", "nsfs", "efivarfs", "selinuxfs",
        "rpc_pipefs", "fuse.gvfsd-fuse", "fuse.portal", "fuse.lxcfs", "iso9660", "devfs",
    }
)
_PSEUDO_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/snap", "/var/lib/docker")

Runner = Callable[[Sequence[str], float, Mapping[str, str]], bytes]


class Probe:
    """The only place that touches psutil. Methods raise on failure."""

    def __init__(self) -> None:
        import psutil

        self._ps = psutil
        self._ps.cpu_percent(interval=None)  # prime: the first reading is meaningless

    def load_avg(self) -> tuple[float, float, float]:
        return self._ps.getloadavg()

    def cpu_percent(self) -> float:
        return self._ps.cpu_percent(interval=None)

    def cpu_count(self) -> int | None:
        return self._ps.cpu_count()

    def virtual_memory(self) -> Any:
        return self._ps.virtual_memory()

    def swap_memory(self) -> Any:
        return self._ps.swap_memory()

    def disk_partitions(self) -> list[Any]:
        return self._ps.disk_partitions(all=False)

    def disk_usage(self, mount: str) -> Any:
        return self._ps.disk_usage(mount)

    def net_io_counters(self) -> Mapping[str, Any]:
        return self._ps.net_io_counters(pernic=True)


def _run_capped(argv: Sequence[str], timeout_s: float, env: Mapping[str, str]) -> bytes:
    """Run argv; return stdout (at most MAX_GPU_OUTPUT + 1 bytes).

    One monotonic deadline covers reads and exit. On deadline or oversize the child's whole
    process group is killed. Raises OSError or subprocess.TimeoutExpired; a nonzero exit
    raises CalledProcessError.
    """
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=dict(env),
        shell=False,
        start_new_session=True,
        bufsize=0,
    )
    deadline = time.monotonic() + timeout_s
    buf = bytearray()
    try:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        with selectors.DefaultSelector() as sel:
            sel.register(fd, selectors.EVENT_READ)
            while len(buf) <= MAX_GPU_OUTPUT:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not sel.select(remaining):
                    raise subprocess.TimeoutExpired(list(argv), timeout_s)
                chunk = os.read(fd, min(65536, MAX_GPU_OUTPUT + 1 - len(buf)))
                if not chunk:
                    break
                buf += chunk
        if len(buf) <= MAX_GPU_OUTPUT:
            code = proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            if code != 0:
                raise subprocess.CalledProcessError(code, list(argv))
        return bytes(buf)
    finally:
        try:  # before reaping, so the group id cannot have been reused
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if proc.stdout is not None:
            proc.stdout.close()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _num(text: str) -> int | float | None:
    try:
        return int(text)
    except ValueError:
        pass
    try:
        val = float(text)
    except ValueError:
        return None
    return val if math.isfinite(val) else None


def _parse_gpus(raw: bytes) -> list[dict]:
    """Parse csv lines `index, name, util, mem_used, mem_total`. Bad lines are skipped."""
    if len(raw) > MAX_GPU_OUTPUT:
        return []
    out: list[dict] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        head, sep, rest = line.partition(",")
        if not sep:
            continue
        name_part, *tail = (s.strip() for s in rest.rsplit(",", 3))
        if len(tail) != 3:
            continue
        nums = [_num(x) for x in (head.strip(), *tail)]
        if any(n is None for n in nums) or not isinstance(nums[0], int) or nums[0] < 0:
            continue
        name = sanitize_text(name_part, _STR_LEN)
        if not name:
            continue
        out.append(
            {
                "index": nums[0],
                "name": name,
                "util": nums[1],
                "mem_used": nums[2],
                "mem_total": nums[3],
            }
        )
        if len(out) >= MAX_ITEMS:
            break
    return out


def _real_fs(part: Any) -> bool:
    if str(part.fstype).lower() in _PSEUDO_FSTYPES or not str(part.fstype):
        return False
    mount = str(part.mountpoint)
    if not mount.startswith("/"):
        return False
    return not any(mount == p or mount.startswith(p + "/") for p in _PSEUDO_PREFIXES)


class HostSource:
    name = "host"

    def __init__(
        self,
        probe: Any | None = None,
        *,
        probe_factory: Callable[[], Any] = Probe,
        nvidia_smi: str | None = "nvidia-smi",
        gpu_timeout_s: float = GPU_TIMEOUT_S,
        runner: Runner = _run_capped,
    ) -> None:
        self._probe = probe
        self._factory = probe_factory
        self._nvidia = nvidia_smi
        self._gpu_timeout = gpu_timeout_s
        self._runner = runner
        self._prev_net: dict[str, tuple[float, int, int]] = {}

    def _get_probe(self) -> Any | None:
        if self._probe is None:
            try:
                self._probe = self._factory()
            except Exception:  # noqa: BLE001 - psutil missing or broken means absent
                return None
        return self._probe

    def _gpus(self) -> list[dict] | None:
        if not self._nvidia:
            return None
        exe = shutil.which(self._nvidia)
        if exe is None:
            return None
        try:
            raw = self._runner([exe, *_GPU_ARGS], self._gpu_timeout, _MIN_ENV)
        except (OSError, subprocess.SubprocessError):
            return None
        return _parse_gpus(raw) or None

    def _disks(self, probe: Any) -> list[dict]:
        disks: list[dict] = []
        for part in sorted(probe.disk_partitions(), key=lambda p: str(p.mountpoint)):
            if len(disks) >= MAX_ITEMS:
                break
            if not _real_fs(part):
                continue
            try:
                u = probe.disk_usage(part.mountpoint)
                disks.append(
                    {
                        "mount": sanitize_text(str(part.mountpoint), _STR_LEN),
                        "total": int(u.total),
                        "used": int(u.used),
                        "percent": float(u.percent),
                    }
                )
            except (OSError, ValueError, TypeError):
                continue  # a mount that vanished or denies access is dropped alone
        return disks

    def _net(self, probe: Any, now: float) -> list[dict]:
        counters = probe.net_io_counters()
        rows: list[dict] = []
        seen: dict[str, tuple[float, int, int]] = {}
        for iface in sorted(counters)[:MAX_ITEMS]:
            c = counters[iface]
            rx, tx = int(c.bytes_recv), int(c.bytes_sent)
            seen[iface] = (now, rx, tx)
            rx_bps = tx_bps = 0.0
            prev = self._prev_net.get(iface)
            if prev is not None:
                dt = now - prev[0]
                if dt > 0 and rx >= prev[1] and tx >= prev[2]:
                    rx_bps = (rx - prev[1]) / dt
                    tx_bps = (tx - prev[2]) / dt
            rows.append(
                {
                    "iface": sanitize_text(str(iface), _STR_LEN),
                    "rx_bps": rx_bps if prev else 0,
                    "tx_bps": tx_bps if prev else 0,
                }
            )
        self._prev_net = seen
        return rows

    def collect(self, ctx: Ctx) -> SourceResult:
        probe = self._get_probe()
        if probe is None:
            return SourceResult(status="absent")
        host: dict[str, Any] = {}
        failures = 0
        attempts = 0

        def attempt(fn: Callable[[], None]) -> None:
            nonlocal failures, attempts
            attempts += 1
            try:
                fn()
            except Exception:  # noqa: BLE001 - one metric failing drops only that metric
                failures += 1

        def load() -> None:
            a, b, c = probe.load_avg()
            host.update(load1=float(a), load5=float(b), load15=float(c))

        def cpu() -> None:
            host["cpu_percent"] = float(probe.cpu_percent())

        def cpus() -> None:
            n = probe.cpu_count()
            if n is not None:
                host["cpu_count"] = int(n)

        def mem() -> None:
            m = probe.virtual_memory()
            host.update(
                mem_total=int(m.total), mem_used=int(m.used), mem_percent=float(m.percent)
            )

        def swap() -> None:
            s = probe.swap_memory()
            host.update(swap_total=int(s.total), swap_used=int(s.used))

        def disks() -> None:
            host["disks"] = self._disks(probe)

        def net() -> None:
            host["net"] = self._net(probe, ctx.clock())

        for fn in (load, cpu, cpus, mem, swap, disks, net):
            attempt(fn)
        if failures == attempts:
            return SourceResult(status="absent")
        gpus = self._gpus()
        if gpus:
            host["gpus"] = gpus
        return SourceResult(host=host)
