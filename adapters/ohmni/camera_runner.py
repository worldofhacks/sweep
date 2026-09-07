"""Run the camera publisher without starting the ground-control runtime."""

from __future__ import annotations

import os
import signal
import threading

from .camera import from_environment


def main() -> None:
    host = os.environ.get("SWEEP_MEDIA_HOST")
    if not host:
        raise SystemExit("camera publisher requires SWEEP_MEDIA_HOST")
    camera = from_environment(host, os.environ.get("SWEEP_NODE_KEY", ""))
    stopped = threading.Event()

    def stop(_signal: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    camera.start()
    stopped.wait()
    camera.close()


if __name__ == "__main__":
    main()
