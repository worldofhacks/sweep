"""Bounded CPU reconstruction worker. Run with `uv run --extra atlas -m tools.atlas_worker`.

Pass --data-dir to the relay's log-dir/atlas. Processing uses a separate child process;
the supervisor maintains a durable heartbeat and enforces a 20-minute wall-clock limit.
No camera, phone or motion-control access is needed.
"""

import argparse
import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from relay.atlas import AtlasStore


def run_one(store: AtlasStore) -> bool:
    job = store.claim_reconstruction()
    if job is None:
        return False
    output = store.root / "reconstructions" / job["id"]
    output.mkdir(parents=True, exist_ok=True)
    process = None
    try:
        with (output / "worker.log").open("wb") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tools.atlas_worker",
                    "--data-dir",
                    str(store.root),
                    "--process-job",
                    job["id"],
                ],
                stdout=log,
                stderr=log,
                env={
                    **os.environ,
                    "OMP_NUM_THREADS": "4",
                    "OPENBLAS_NUM_THREADS": "4",
                    "OPENCV_IO_MAX_IMAGE_PIXELS": "40000000",
                },
            )
            deadline = time.monotonic() + 1200
            while process.poll() is None:
                if time.monotonic() > deadline:
                    process.kill()
                    process.wait()
                    store.progress_reconstruction(
                        job["id"],
                        status="failed",
                        detail="The build exceeded 20 minutes. Try fewer overlapping captures.",
                    )
                    break
                # A failed lease cannot be revived by an old worker.
                current = store.reconstruction_job(job["id"])
                if current["status"] == "failed":
                    process.kill()
                    process.wait()
                    break
                store.progress_reconstruction(job["id"])
                time.sleep(2)
            if process.returncode != 0:
                store.progress_reconstruction(
                    job["id"],
                    status="failed",
                    detail="The worker stopped early. Originals are intact; try a new build.",
                )
            elif store.reconstruction_job(job["id"])["status"] != "ready":
                store.progress_reconstruction(
                    job["id"],
                    status="failed",
                    detail="The worker did not publish a verified artifact. Try a new build.",
                )
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            store.progress_reconstruction(
                job["id"],
                status="failed",
                detail="The worker was stopped. Start a new build when it is available.",
            )
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--process-job", help=argparse.SUPPRESS)
    args = parser.parse_args()

    def stop(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    if importlib.util.find_spec("pycolmap") is None:
        parser.error("Install the optional engine: uv sync --extra atlas")
    store = AtlasStore(args.data_dir.resolve())
    try:
        if args.process_job:
            from spatial.atlas_reconstruction import reconstruct

            job = store.reconstruction_job(args.process_job)
            if job["status"] != "preparing":
                parser.error("This job is not assigned to a worker.")
            try:
                reconstruct(store, job)
            except Exception as error:
                detail = (
                    str(error)
                    if isinstance(error, ValueError)
                    else "The engine could not process these captures. Check the worker log "
                    "and add clearer overlapping views."
                )
                store.progress_reconstruction(job["id"], status="failed", detail=detail[:1000])
                raise
        else:
            while True:
                processed = run_one(store)
                if args.once:
                    break
                if not processed:
                    time.sleep(2)
    finally:
        store.close()


if __name__ == "__main__":
    main()
