"""Loopback-only screen controls: a synchronous hardware STOP and a spotter claim."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from nodekit.node import Node

from .device import OhmniDevice


def serve_screen(node: Node, device: OhmniDevice, port: int = 8765) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def reply(self, code: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; "
                "script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "connect-src 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                self.reply(
                    200,
                    Path(__file__).with_name("screen").joinpath("index.html").read_bytes(),
                    "text/html; charset=utf-8",
                )
            elif self.path == "/status":
                status = device.status()
                self.reply(
                    200,
                    json.dumps(
                        {
                            "device_id": node.config.device_id,
                            "connected": node.connection_epoch is not None,
                            "authority": status.control_authority,
                            "watchdog": node.watchdog_state,
                            "spotter": device.spotter_present,
                            "guard": status.extras["obstacle_guard"],
                            "lidar": status.extras["lidar_present"],
                            "calibrated": status.extras["lidar_calibrated"],
                            "video": device.video_publish_state(),
                            "last_refusal": device.last_refusal or node.last_refusal,
                        }
                    ).encode(),
                )
            else:
                self.reply(404, b"{}")

        def do_POST(self) -> None:
            # A page from another origin cannot silently claim a spotter/re-enable.
            host = self.headers.get("Host", "")
            expected_hosts = {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }
            origin = self.headers.get("Origin")
            if host not in expected_hosts or (origin is not None and origin != f"http://{host}"):
                self.reply(403, b"{}")
                return
            if self.headers.get("X-Sweep-Local") != "1":
                self.reply(403, b"{}")
                return
            if self.path == "/stop":
                # Do this before queueing any relay update, even if its loop has stalled.
                if not node.local_stop():
                    self.reply(503, b'{"error":"hardware_stop_unconfirmed"}')
                    return
            elif self.path == "/spotter":
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 64:
                        raise ValueError("invalid body length")
                    present = json.loads(self.rfile.read(size)).get("present")
                    if not isinstance(present, bool):
                        raise ValueError("present must be boolean")
                except (ValueError, AttributeError):
                    self.reply(400, b"{}")
                    return
                node.set_safety_operator_present(present)
                try:
                    device.set_spotter_present(present)
                except OSError:
                    node.local_stop()
                    self.reply(503, b'{"error":"hardware_stop_unconfirmed"}')
                    return
            elif self.path == "/enable":
                if not device.spotter_present:
                    self.reply(409, b'{"error":"spotter_missing"}')
                    return
                node.request_reenable()
            else:
                self.reply(404, b"{}")
                return
            self.reply(200, b'{"ok":true}')

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="ohmni-screen", daemon=True)
    thread.start()
    return server
