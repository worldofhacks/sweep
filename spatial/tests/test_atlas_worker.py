"""Worker lifecycle checks use only disposable local child processes, never devices."""

import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from relay.atlas import AtlasStore
from relay.tests.test_atlas_reconstruction import seed
from tools import atlas_worker
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


def test_workspace_watchdog_counts_nested_files_without_following_links(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "nested").mkdir()
    (work / "nested" / "frame").write_bytes(b"x" * 60)
    (work / "features.db").write_bytes(b"x" * 40)
    originals = tmp_path / "originals"
    originals.mkdir()
    (originals / "photo").write_bytes(b"x" * 1_000)
    (work / "source-link").symlink_to(originals, target_is_directory=True)
    (work / "file-link").symlink_to(originals / "photo")
    monkeypatch.setattr(atlas_worker, "MAX_WORK_BYTES", 100)
    assert atlas_worker.workspace_limit(work) is None
    (work / "extra").write_bytes(b"x")
    assert "working files" in atlas_worker.workspace_limit(work)
    assert (originals / "photo").stat().st_size == 1_000


def test_workspace_watchdog_bounds_file_count(tmp_path, monkeypatch):
    monkeypatch.setattr(atlas_worker, "MAX_WORK_ENTRIES", 3)
    for index in range(3):
        (tmp_path / str(index)).touch()
    assert atlas_worker.workspace_limit(tmp_path) is None
    (tmp_path / "extra").touch()
    assert "generated-file limit" in atlas_worker.workspace_limit(tmp_path)


def test_workspace_watchdog_accepts_removed_intermediates(tmp_path, monkeypatch):
    directory = tmp_path / "intermediate"
    directory.mkdir()
    scan = os.scandir

    def disappearing(path):
        if Path(path) == directory:
            directory.rmdir()
        return scan(path)

    monkeypatch.setattr(atlas_worker.os, "scandir", disappearing)
    assert atlas_worker.workspace_limit(tmp_path) is None


@pytest.fixture
def queued_job(tmp_path):
    store = AtlasStore(tmp_path / "atlas")
    identifier = seed(store)
    job = store.queue_reconstruction(identifier)
    originals = {path.name: path.read_bytes() for path in store.media.iterdir()}
    output = store.root / "reconstructions" / job["id"]
    output.mkdir(parents=True)
    # Previously retained data is outside the newly owned scratch directory.
    retained = output / "processing-older"
    retained.mkdir()
    (retained / "diagnostic").write_bytes(b"keep")
    try:
        yield store, job, output
    finally:
        assert {path.name: path.read_bytes() for path in store.media.iterdir()} == originals
        assert (retained / "diagnostic").read_bytes() == b"keep"
        store.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX child-process ownership")
@pytest.mark.parametrize(
    "outcome", ["ready", "failed", "killed", "quota", "no_artifact", "timeout", "inspection_error"]
)
def test_supervisor_cleans_only_its_scratch_after_real_child_exit(queued_job, monkeypatch, outcome):
    store, job, output = queued_job
    popen = subprocess.Popen
    children = []
    scratch_paths = []
    monkeypatch.setattr(atlas_worker, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(atlas_worker, "MAX_WORK_BYTES", 2_000_000)
    if outcome == "timeout":
        clock = iter([0, 1201])
        monkeypatch.setattr(
            atlas_worker, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=time.sleep)
        )
    elif outcome == "inspection_error":

        def cannot_inspect(_directory):
            raise OSError("Cannot inspect generated files")

        monkeypatch.setattr(atlas_worker, "workspace_limit", cannot_inspect)

    def child(arguments, **kwargs):
        scratch = Path(arguments[arguments.index("--scratch-dir") + 1])
        scratch_paths.append(scratch)
        # Only lifecycle fixtures, not generated geometry or engine acceptance.
        program = (
            "import os,signal,time; from pathlib import Path; "
            "from relay.atlas import AtlasStore; "
            f"scratch=Path({str(scratch)!r}); "
            f"output=Path({str(output)!r}); "
            "(scratch/'engine-depth').write_bytes(b'x'*64); "
            "(output/'retained-camera-solution').write_bytes(b'measured-fixture'); "
            f"store=AtlasStore(Path({str(store.root)!r})); "
        )
        if outcome == "ready":
            program += f"store.progress_reconstruction({job['id']!r},status='ready'); store.close()"
        elif outcome == "failed":
            program += (
                f"store.progress_reconstruction({job['id']!r},status='failed',"
                "detail='Not enough overlapping views.'); store.close(); raise SystemExit(3)"
            )
        elif outcome == "killed":
            program += "store.close(); os.kill(os.getpid(),signal.SIGKILL)"
        elif outcome == "quota":
            program += (
                "store.close(); (scratch/'depth').write_bytes(b'x'*2_000_001); time.sleep(60)"
            )
        elif outcome in ("timeout", "inspection_error"):
            program += "store.close(); time.sleep(60)"
        else:
            program += "store.close()"
        process = popen([sys.executable, "-c", program], **kwargs)
        children.append(process)
        if outcome in ("timeout", "inspection_error"):
            deadline = time.monotonic() + 5
            while not (output / "retained-camera-solution").exists():
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(0.01)
        return process

    monkeypatch.setattr(atlas_worker.subprocess, "Popen", child)
    try:
        if outcome == "inspection_error":
            with pytest.raises(OSError, match="Cannot inspect generated files"):
                atlas_worker.run_one(store)
        else:
            assert atlas_worker.run_one(store)
        current = store.reconstruction_job(job["id"])
        assert current["worker_released"] is True
        assert current["status"] == ("ready" if outcome == "ready" else "failed")
        if outcome == "failed":
            assert current["detail"] == "Not enough overlapping views."
        elif outcome == "quota":
            assert "working files" in current["detail"]
        elif outcome == "no_artifact":
            assert "did not publish" in current["detail"]
        elif outcome == "timeout":
            assert "20 minutes" in current["detail"]
        assert (output / "retained-camera-solution").read_bytes() == b"measured-fixture"
        assert scratch_paths and all(not path.exists() for path in scratch_paths)
        assert all(process.poll() is not None for process in children)
    finally:
        for process in children:
            if process.poll() is None:
                stop_process_tree(process)


def test_supervisor_fails_spawn_error_and_cleans_owned_scratch(queued_job, monkeypatch):
    store, job, output = queued_job

    def fail(*_args, **_kwargs):
        raise OSError("Cannot start worker")

    monkeypatch.setattr(atlas_worker.subprocess, "Popen", fail)
    with pytest.raises(OSError, match="Cannot start worker"):
        atlas_worker.run_one(store)
    assert store.reconstruction_job(job["id"])["status"] == "failed"
    assert store.reconstruction_job(job["id"])["worker_released"] is True
    assert sorted(path.name for path in output.iterdir()) == ["processing-older", "worker.log"]


def test_scratch_allocation_failure_does_not_leave_active_lease(queued_job, monkeypatch):
    store, job, output = queued_job

    def fail(*_args, **_kwargs):
        raise OSError("No scratch storage")

    monkeypatch.setattr(atlas_worker.tempfile, "TemporaryDirectory", fail)
    with pytest.raises(OSError, match="No scratch storage"):
        atlas_worker.run_one(store)
    assert store.reconstruction_job(job["id"])["status"] == "failed"
    assert sorted(path.name for path in output.iterdir()) == ["processing-older"]
