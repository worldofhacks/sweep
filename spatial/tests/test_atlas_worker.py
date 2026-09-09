"""Worker lifecycle checks use only disposable local child processes, never devices."""

import os
import select
import signal
import subprocess
import sys

import pytest

from tools.atlas_worker import stop_process_tree


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group ownership")
def test_stop_process_tree_stops_engine_even_after_worker_exits():
    # An inherited pipe stays open while either process lives. The engine ignores
    # TERM to exercise the required KILL of survivors after the worker has exited.
    engine = (
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready',flush=True); time.sleep(60)"
    )
    worker = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable,'-c',{engine!r}]); "
        "print('worker exits',flush=True)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", worker], stdout=subprocess.PIPE, start_new_session=True
    )
    try:
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 10)[0]
        assert process.stdout.readline() == b"worker exits\n"
        assert select.select([process.stdout], [], [], 10)[0]
        assert process.stdout.readline() == b"ready\n"
        assert process.wait(timeout=5) == 0
        stop_process_tree(process)
        assert select.select([process.stdout], [], [], 5)[0]
        assert process.stdout.read() == b""
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()
