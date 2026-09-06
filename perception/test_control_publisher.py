import itertools
import threading
import time

import pytest

from perception.control_publisher import (
    ControlPublisher,
    ControlPublisherConfig,
    _host_boot_id,
    _run_live,
)
from relay.control_frames import ControlLocalizationFrame
from relay.control_localization import (
    ClockMapping,
    ControlLocalizationPins,
    ControlLocalizationProjector,
)


class FakeTransport:
    def __init__(self):
        self.authenticated = []
        self.frames = []

    def authenticate(self, drone_id, token):
        self.authenticated.append((drone_id, token))

    def send(self, frame):
        self.frames.append(dict(frame))


def config(mode="replay"):
    result = {
        "mode": mode,
        "session": "session-1",
        "websocket_url": None if mode == "replay" else "ws://relay.example/ws",
        "drones": [
            {
                "key_environment": "LOCALIZATION_KEY_1",
                "max_position_uncertainty_m": 0.3,
                "clock_mapping": {
                    "capture_clock_id": "camera-clock",
                    "relay_clock_id": "relay-clock",
                    "capture_reference_s": 0,
                    "relay_reference_ms": 100_000,
                    "milliseconds_per_capture_second": 1000,
                    "max_error_ms": 5,
                    "measured": True,
                },
                "fuser": {
                    "drone_id": 1,
                    "connection_epoch": 1,
                    "map_id": "map-sha",
                    "geometry_id": "geometry-sha",
                    "clock_id": "camera-clock",
                    "tag_source_id": "tag-camera",
                    "velocity_source_id": "msdk-velocity",
                    "height_source_id": "tof-height",
                    "camera_calibration_id": "camera-sha",
                    "body_extrinsics_id": "body-sha",
                    "position_bounds_map_enu_m": [[-10, 10], [-10, 10], [0, 3]],
                    "height_bounds_map_enu_m": [0, 3],
                    "max_speed_mps": 5,
                    "position_variance_bounds_m2": [0.000001, 0.0625],
                    "velocity_variance_bounds_m2ps2": [0.000001, 1],
                    "height_variance_bounds_m2": [0.000001, 0.0625],
                    "production_evidence_verified": True,
                },
            }
        ],
    }
    if mode == "live":
        result["drones"][0]["live_capture_clock"] = {
            "source": "process_monotonic",
            "boot_id": _host_boot_id(),
            "monotonic_reference_s": 10.0,
            "capture_reference_s": 0.0,
        }
    return result


def tag(now_s=1.0):
    return {
        "kind": "tag",
        "drone_id": 1,
        "event_id": "tag",
        "connection_epoch": 1,
        "map_id": "map-sha",
        "geometry_id": "geometry-sha",
        "clock_id": "camera-clock",
        "capture_time": 0.9,
        "position_map_enu_m": [1, 2, 1],
        "covariance_map_enu_m2": [[0.01, 0, 0], [0, 0.01, 0], [0, 0, 0.01]],
        "source_id": "tag-camera",
        "camera_calibration_id": "camera-sha",
        "source_verified": True,
        "timing_verified": True,
        "now_s": now_s,
        "extrinsics": {
            "extrinsics_id": "body-sha",
            "source_id": "tag-camera",
            "matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            "capture_time": 0.9,
            "gimbal_time": 0.9,
            "attitude_time": 0.9,
            "measured": True,
        },
    }


def velocity():
    return {
        "kind": "velocity",
        "drone_id": 1,
        "event_id": "velocity",
        "connection_epoch": 1,
        "map_id": "map-sha",
        "geometry_id": "geometry-sha",
        "clock_id": "camera-clock",
        "capture_time": 0.95,
        "velocity_map_enu_mps": [0, 0, 0],
        "covariance_m2ps2": [[0.01, 0, 0], [0, 0.01, 0], [0, 0, 0.01]],
        "source_id": "msdk-velocity",
        "source_verified": True,
        "timing_verified": True,
    }


def height():
    return {
        "kind": "height",
        "drone_id": 1,
        "event_id": "height",
        "connection_epoch": 1,
        "map_id": "map-sha",
        "geometry_id": "geometry-sha",
        "clock_id": "camera-clock",
        "capture_time": 0.97,
        "height_map_enu_m": 1,
        "variance_m2": 0.01,
        "source_id": "tof-height",
        "source_verified": True,
        "timing_verified": True,
    }


def test_jsonl_records_drive_fuser_and_emit_signed_replay_frame():
    publisher = ControlPublisher(ControlPublisherConfig.from_mapping(config()))
    for record in [velocity(), height(), tag()]:
        publisher.enqueue(record)
    frame = publisher.publish(1, 1.0)
    localization = ControlLocalizationFrame.parse(frame)
    assert localization.wire.control_eligible
    assert frame["t"] == 101_000
    assert localization.signature_valid(b"replay")


@pytest.mark.parametrize(
    "field",
    [
        "position_bounds_map_enu_m",
        "height_bounds_map_enu_m",
        "max_speed_mps",
        "position_variance_bounds_m2",
        "velocity_variance_bounds_m2ps2",
        "height_variance_bounds_m2",
    ],
)
def test_publisher_rejects_a_deployment_missing_a_required_fuser_limit(field):
    raw = config()
    del raw["drones"][0]["fuser"][field]

    with pytest.raises(ValueError, match="fuser configuration"):
        ControlPublisherConfig.from_mapping(raw)


def test_publisher_frame_projects_through_the_signed_diagnostic_transport():
    publisher = ControlPublisher(ControlPublisherConfig.from_mapping(config()))
    for record in [velocity(), height(), tag()]:
        publisher.enqueue(record)
    frame = publisher.publish(1, 1.0)
    localization = ControlLocalizationFrame.parse(frame)
    mapping = ClockMapping("camera-clock", "relay-clock", 0, 100_000, 1000, 5, True)
    projector = ControlLocalizationProjector(
        {
            1: ControlLocalizationPins(
                1,
                "map-sha",
                "geometry-sha",
                "camera-sha",
                "body-sha",
                ("tag-camera", "msdk-velocity", "tof-height"),
                mapping,
            )
        },
        relay_clock_id="relay-clock",
        max_clock_error_ms=5,
        max_fix_age_ms=500,
        max_velocity_age_ms=500,
        max_height_age_ms=500,
        max_position_uncertainty_p95_m=0.3,
    )

    assert localization.signature_valid(b"replay")
    pose = projector.project(
        localization.wire,
        authenticated_drone_id=1,
        authenticated_connection_epoch=1,
        now_ms=101_000,
        event_id="diagnostic-1",
        session="session-1",
        previous=None,
    )

    assert pose.status == "ready"
    assert pose.flight_approved is False
    assert pose.fix_time_ms == 100_895


def test_live_authenticates_distinct_source_and_stale_frames_hold_without_replay():
    transport = FakeTransport()
    publisher = ControlPublisher(ControlPublisherConfig.from_mapping(config("live")), transport)
    publisher.bind_live_credentials({"LOCALIZATION_KEY_1": "key"})
    assert transport.authenticated == [(1, "key")]
    initial = publisher.publish_live(1, 12.1)
    landed = publisher.publish_live(1, 15.1)
    assert initial["localization_status"] == "hold"
    assert landed["localization_status"] == "land"
    assert initial["control_eligible"] is False
    assert len(transport.frames) == 2


def test_live_capture_clock_uses_the_calibrated_monotonic_reference_not_record_time():
    transport = FakeTransport()
    publisher = ControlPublisher(ControlPublisherConfig.from_mapping(config("live")), transport)
    publisher.bind_live_credentials({"LOCALIZATION_KEY_1": "key"})
    publisher.enqueue(velocity())
    publisher.enqueue(height())
    publisher.enqueue(tag(now_s=999_999))

    frame = publisher.publish_live(1, 11.0)

    assert frame["t"] == 101_000
    assert frame["localization_status"] == "ready"

    with pytest.raises(ValueError, match="calibrated capture clock"):
        publisher.publish(1, 999_999)


def test_live_rejects_a_capture_clock_from_another_process_boot():
    raw = config("live")
    raw["drones"][0]["live_capture_clock"]["boot_id"] = "previous-process-boot"
    publisher = ControlPublisher(ControlPublisherConfig.from_mapping(raw), FakeTransport())

    with pytest.raises(ValueError, match="boot ID"):
        publisher.bind_live_credentials({"LOCALIZATION_KEY_1": "key"})


def test_live_requires_a_measured_capture_clock_with_the_mapping_reference():
    missing = config("live")
    del missing["drones"][0]["live_capture_clock"]
    with pytest.raises(ValueError, match="publisher configuration"):
        ControlPublisherConfig.from_mapping(missing)

    mismatched = config("live")
    mismatched["drones"][0]["live_capture_clock"]["capture_reference_s"] = 1.0
    with pytest.raises(ValueError, match="publisher drone configuration"):
        ControlPublisherConfig.from_mapping(mismatched)


def test_live_loop_publishes_hold_then_land_while_input_stalls():
    class StalledLines:
        def __init__(self) -> None:
            self.release = threading.Event()

        def __iter__(self) -> "StalledLines":
            return self

        def __next__(self) -> str:
            self.release.wait()
            raise StopIteration

    transport = FakeTransport()
    publisher = ControlPublisher(ControlPublisherConfig.from_mapping(config("live")), transport)
    publisher.bind_live_credentials({"LOCALIZATION_KEY_1": "key"})
    lines = StalledLines()
    ticks = itertools.chain([10.0, 10.0, 10.0, 13.0, 13.0, 13.0], itertools.repeat(13.0))
    waits = 0

    def wait(_seconds: float) -> None:
        nonlocal waits
        waits += 1
        if waits == 2:
            lines.release.set()
        time.sleep(0.001)

    _run_live(publisher, lines, clock=lambda: next(ticks), wait=wait)

    assert [frame["localization_status"] for frame in transport.frames[:2]] == ["hold", "land"]


def test_live_loop_propagates_invalid_json_without_turning_it_into_a_hold():
    transport = FakeTransport()
    publisher = ControlPublisher(ControlPublisherConfig.from_mapping(config("live")), transport)
    publisher.bind_live_credentials({"LOCALIZATION_KEY_1": "key"})

    with pytest.raises(ValueError):
        _run_live(
            publisher,
            ["not json"],
            clock=itertools.repeat(10.0).__next__,
            wait=lambda _seconds: time.sleep(0.001),
        )


def test_bounded_queue_drops_old_unprocessed_records_and_refuses_bad_webcam_evidence():
    raw = config()
    raw["queue_limit"] = 1
    publisher = ControlPublisher(ControlPublisherConfig.from_mapping(raw))
    publisher.enqueue(tag())
    bad = tag()
    bad["event_id"] = "estimated-webcam"
    bad["timing_verified"] = False
    publisher.enqueue(bad)
    frame = publisher.publish(1, 1.0)
    assert frame["control_eligible"] is False
    assert frame["localization_status"] == "hold"
