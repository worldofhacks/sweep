"""Lifecycle acceptance with owned disposable child processes, no reconstruction engine."""

import os
import subprocess
import sys
import time

import pytest

from relay.atlas import AtlasStore
from relay.tests.test_atlas_reconstruction import seed
from relay.tests.test_atlas_removal import withdraw
from tools import atlas_worker


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group shutdown acceptance")
@pytest.mark.parametrize("stop_fails", [False, True])
def test_withdrawal_waits_for_real_worker_shutdown(tmp_path, monkeypatch, stop_fails):
    store = AtlasStore(tmp_path / "atlas")
    sid = seed(store)
    captures = store.captures(sid)
    cid = captures[0]["id"]
    job = store.queue_reconstruction(sid)
    output = store.root / "reconstructions" / job["id"]
    real_popen, real_stop = subprocess.Popen, atlas_worker.stop_process_tree
    children = []
    monkeypatch.setattr(atlas_worker, "POLL_SECONDS", 0.01)

    def child(_arguments, **kwargs):
        process = real_popen(
            [
                sys.executable,
                "-c",
                "import time; from pathlib import Path; "
                f"Path({str(output / 'private-derivative')!r}).write_bytes(b'private fixture'); "
                "time.sleep(60)",
            ],
            **kwargs,
        )
        children.append(process)
        deadline = time.monotonic() + 5
        while not (output / "private-derivative").exists():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        withdraw(store, sid, cid)
        store.removal.cleanup()
        assert store.removal.preview(sid, cid, operator=True)["state"] == "cleanup_pending"
        assert process.poll() is None
        assert output.exists()
        return process

    monkeypatch.setattr(atlas_worker.subprocess, "Popen", child)
    if stop_fails:

        def cannot_confirm(_process):
            raise OSError("Unable to confirm process shutdown")

        monkeypatch.setattr(atlas_worker, "stop_process_tree", cannot_confirm)
    try:
        if stop_fails:
            with pytest.raises(OSError, match="Unable to confirm"):
                atlas_worker.run_one(store)
            assert not store.reconstruction_job(job["id"])["worker_released"]
            store.removal.cleanup()
            assert store.removal.preview(sid, cid, operator=True)["state"] == "cleanup_pending"
            assert output.exists()
        else:
            assert atlas_worker.run_one(store)
            assert all(process.poll() is not None for process in children)
            assert store.reconstruction_job(job["id"])["worker_released"]
            assert store.removal.preview(sid, cid, operator=True)["state"] == "local_removed"
            assert not output.exists()
        assert not (store.media / cid).exists()
        assert all((store.media / capture["id"]).exists() for capture in captures[1:])
    finally:
        for process in children:
            real_stop(process)
        store.close()
