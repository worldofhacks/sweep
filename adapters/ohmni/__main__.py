"""Direct Android launcher; only stdlib and nodekit's websockets dependency are needed."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from nodekit.node import Node, NodeConfig

from .device import from_environment
from .screen import serve_screen


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    key_path = os.environ.get("SWEEP_NODE_KEY_FILE")
    key = Path(key_path).read_text().strip() if key_path else os.environ["SWEEP_NODE_KEY"]
    device_id = int(os.environ["SWEEP_DEVICE_ID"])
    config = NodeConfig(
        relay_url=os.environ["SWEEP_RELAY_URL"].rstrip("/"),
        session=os.environ["SWEEP_SESSION_ID"],
        device_id=device_id,
        token=key,
        adapter_id=os.environ.get("SWEEP_ADAPTER_ID", f"ohmni-{device_id}"),
        safety_operator_present=os.environ.get("SWEEP_SPOTTER") == "1",
        home_pose_confirmed=os.environ.get("SWEEP_HOME_CONFIRMED") == "1",
    )
    device = from_environment(key=key)
    node = Node(config, device)
    screen = serve_screen(node, device)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        # Node.stop joins its optional worker thread; this launcher runs on this loop.
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, node.stop)
        await node.run()

    try:
        asyncio.run(run())
    finally:
        screen.shutdown()
        screen.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
