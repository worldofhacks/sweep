"""Run the camera publisher without starting the ground-control runtime."""

from __future__ import annotations

import argparse
import json
import os
import signal
import threading

from .camera import from_environment


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true", help="one capture/publication attempt")
    parser.add_argument(
        "--timeout", type=float, default=10.0, help="probe deadline in seconds (1–30)"
    )
    arguments = parser.parse_args(argv)
    host = os.environ.get("SWEEP_MEDIA_HOST")
    if not host:
        raise SystemExit("camera publisher requires SWEEP_MEDIA_HOST")
    camera = from_environment(host, os.environ.get("SWEEP_NODE_KEY", ""))
    stopped = threading.Event()

    def stop(_signal: int, _frame: object) -> None:
        stopped.set()
        camera.request_stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if arguments.probe:
            result = camera.probe(arguments.timeout)
            print(json.dumps(result), flush=True)
            raise SystemExit(0 if result["status"] == "frames_observed" else 1)
        camera.start()
        stopped.wait()
    finally:
        camera.close()


if __name__ == "__main__":
    main()
