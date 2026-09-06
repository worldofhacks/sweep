"""Console intents traverse the production relay and both node classes over sockets."""

import cv2
import httpx
import numpy as np
import pytest

from adapters.dji_mini3.fake_node import FakeNode, FakeNodeConfig
from nodekit.fake import FakeGroundVehicle
from nodekit.node import Node, NodeConfig
from planner.models import DeviceClass
from relay.tests.conftest import CONSOLE_KEY, SESSION
from relay.tests.test_autonomy_roundtrip import (
    _Fleet,
    _wait_until,
    relay_server,
)

# Reuse the real autonomous relay fixture, supplying its keys for five physical slots.
# Pytest imports fixtures explicitly under importlib mode.
assert relay_server


@pytest.fixture
def mixed_server(tmp_path, monkeypatch):
    import relay.tests.test_autonomy_roundtrip as support

    keys = {
        device_id: f"fixture-only-mixed-device-{device_id}-key-32-bytes".encode()
        for device_id in (1, 2, 11, 12, 13)
    }
    original = support.RelaySettings

    def settings(**values):
        values["adapter_keys"] = keys
        values["device_classes"] = {
            device_id: DeviceClass.GROUND_VEHICLE for device_id in (11, 12, 13)
        }
        return original(**values)

    monkeypatch.setattr(support, "RelaySettings", settings)
    # The test's odometry fixture has 0.95 quality, satisfying the unchanged safety
    # threshold. This is synthetic evidence and says nothing about wheel-odometry drift.
    yield from relay_server.__wrapped__(tmp_path)


def test_individual_subset_and_full_fleet_controls_with_live_sensor_map(mixed_server):
    server = mixed_server
    keys = server.runtime.settings.adapter_keys
    fleet = _Fleet(server, {})
    fleet.ids = [1, 2, 11, 12, 13]
    robots = {
        device_id: FakeGroundVehicle(
            start=((device_id - 11) * 2.0, 3.0, 0.0),
            room=(-5.0, -5.0, 7.0, 7.0),
            pos_quality=0.95,
            capabilities=("ground_drive", "camera")
            if device_id == 13
            else ("ground_drive", "lidar", "camera"),
        )
        for device_id in (11, 12, 13)
    }
    fleet.nodes = [
        FakeNode(
            FakeNodeConfig(
                relay_url=server.url,
                session=SESSION,
                drone_id=device_id,
                token=keys[device_id].decode(),
                adapter_id=f"fixture-aircraft-{device_id}",
                home=((device_id - 1) * 2.0, 0.0, 0.0),
                telemetry_hz=5.0,
            )
        )
        for device_id in (1, 2)
    ] + [
        Node(
            NodeConfig(
                relay_url=server.url,
                session=SESSION,
                device_id=device_id,
                token=keys[device_id].decode(),
                adapter_id=f"fixture-ground-{device_id}",
                telemetry_hz=5.0,
            ),
            robot,
        )
        for device_id, robot in robots.items()
    ]

    def run(name, selection, **kwargs):
        intent_id, outcome = fleet.run(name, selection=selection, **kwargs)
        assert outcome.get("status") == "completed", str(outcome)
        return intent_id

    def select(ids):
        run("select", [], args={"ids": ids})
        _wait_until(
            lambda: server.runtime.sessions[SESSION].current_state()["selection"] == ids,
            what="authoritative mixed selection",
        )

    try:
        fleet.start()
        run("arm", [])
        select([1, 2])
        run("takeoff", [1, 2], confirm=True)
        _wait_until(
            lambda: all(fleet.telemetry(i, "state") == "hovering" for i in (1, 2)),
            what="aircraft takeoff telemetry",
        )

        select([11])
        individual = run("translate", [11], args={"dx": 0.2, "dy": 0})
        _wait_until(lambda: abs(robots[11].x - 0.1) < 0.01, what="individual robot motion")
        assert robots[12].x == 2.0 and robots[13].x == 4.0
        _, refused = fleet.run("takeoff", selection=[11], confirm=True)
        assert refused["reason"] == "unsupported_for_device_class"

        select([1, 12])
        subset = run("translate", [1, 12], args={"dx": 0, "dy": 0.2})
        _wait_until(lambda: abs(robots[12].y - 3.1) < 0.01, what="subset robot motion")
        assert robots[11].y == 3.0 and robots[13].y == 3.0

        select(fleet.ids)
        full = run("translate", fleet.ids, args={"dx": 0, "dy": 0.2})
        run("hold", fleet.ids)
        for device_id in (11, 12):
            scan = fleet.console.wait_for("sensor", drone_id=device_id)
            assert len(scan["ranges_cm"]) == 360
        assert not any(
            event.get("type") == "sensor" and event.get("drone_id") == 13
            for event in fleet.console.events
        ), "a missing lidar kit must never produce synthetic live scans"

        response = httpx.get(
            f"http://127.0.0.1:{server.port}/api/sessions/{SESSION}/map",
            headers={"Authorization": f"Bearer {CONSOLE_KEY.decode()}"},
        )
        assert response.status_code == 200
        raster = cv2.imdecode(np.frombuffer(response.content, np.uint8), cv2.IMREAD_UNCHANGED)
        assert raster is not None and raster.ndim == 2
        assert 0 in raster and 255 in raster

        # A fleet landing targets aircraft while robots remain stationary and enabled.
        landed = run("land_all", [], confirm=True)
        assert all(robot.enabled for robot in robots.values())
        stopped = run("estop", [])
        _wait_until(lambda: all(not robot.enabled for robot in robots.values()), what="robot stop")
    finally:
        fleet.stop()

    events = [row["event"] for row in server.runtime.replay(SESSION)["events"]]
    commands = [event for event in events if event["type"] == "command"]
    for intent_id, expected in (
        (individual, {11}),
        (subset, {1, 12}),
        (full, set(fleet.ids)),
        (landed, {1, 2}),
        (stopped, set(fleet.ids)),
    ):
        assert {c["drone_id"] for c in commands if c["intent_id"] == intent_id} == expected
    assert all(c["args"].get("z_mm", 0) == 0 for c in commands if c["drone_id"] >= 11)
