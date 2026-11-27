"""The shared pid_alive/wait_gone helpers: a zombie is gone, a running process is alive."""

import os
import signal
import subprocess
import sys

from conftest import pid_alive, wait_gone


def test_running_process_reads_alive_and_wait_gone_times_out():
    p = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    try:
        assert pid_alive(p.pid)
        assert wait_gone([p.pid], 0.2) is False
    finally:
        p.kill()
        p.wait()


def test_unreaped_zombie_reads_gone():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOWAIT)  # exited, deliberately not reaped
        os.kill(p.pid, 0)  # the raw probe still succeeds for a zombie
        assert not pid_alive(p.pid)
        assert wait_gone([p.pid], 0.5)
    finally:
        p.wait()


def test_reaped_pid_reads_gone():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not pid_alive(p.pid)


def test_killed_then_unreaped_reads_gone():
    p = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    os.kill(p.pid, signal.SIGKILL)
    try:
        os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOWAIT)
        assert wait_gone([p.pid], 1.0)
    finally:
        p.wait()
