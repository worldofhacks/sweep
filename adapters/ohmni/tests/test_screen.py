from __future__ import annotations

import json
import urllib.error
import urllib.request

from adapters.ohmni.device import Config, OhmniDevice
from adapters.ohmni.screen import serve_screen
from nodekit.node import Node, NodeConfig

from .test_device import Shell


def test_local_stop_disables_without_relay_and_spotter_cannot_be_claimed_cross_origin():
    robot = OhmniDevice(Config(), shell_factory=Shell, lidar_discover=lambda: None, autostart=False)
    node = Node(NodeConfig("ws://localhost", "test", 11, "test-key", "test"), robot)
    server = serve_screen(node, robot, port=0)
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(origin + "/status") as response:
            state = json.load(response)
        assert state["connected"] is False and state["authority"] is False
        stop = urllib.request.Request(
            origin + "/stop",
            method="POST",
            data=b"{}",
            headers={"X-Sweep-Local": "1", "Origin": origin},
        )
        with urllib.request.urlopen(stop) as response:
            assert response.status == 200
        assert node._failsafe_latched and not robot.enabled
        assert robot.drive_shell.commands[-2:] == ["manual_move 0 0", "sleep"]
        remote = urllib.request.Request(
            origin + "/spotter",
            method="POST",
            data=b'{"present":true}',
            headers={"X-Sweep-Local": "1", "Origin": "https://other.test"},
        )
        try:
            urllib.request.urlopen(remote)
            raise AssertionError("cross-origin spotter claim must be rejected")
        except urllib.error.HTTPError as error:
            assert error.code == 403
        assert robot.spotter_present is False
    finally:
        server.shutdown()
        server.server_close()
