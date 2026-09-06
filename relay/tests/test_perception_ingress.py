from __future__ import annotations

from dataclasses import dataclass

from perception.detection_publisher import DetectionPublisher, DetectionPublisherConfig
from perception.object_detection import FrameIdentity, ProcessedFrameEvent
from perception.search_events import CoverageObservation
from planner.control_provenance import ControlProvenance
from planner.navigation import Pose
from relay.auth import Principal, sign_event
from relay.control_localization import ClockMapping
from relay.perception_ingress import (
    DetectionDroneState,
    DetectionIngress,
    DetectionIngressConfig,
    DetectionSourcePin,
    TrustedCapturePose,
)


@dataclass
class _Runtime:
    calls: list[tuple[str, ProcessedFrameEvent, object]]

    def observe_processed_frame(self, intent_id: str, event: ProcessedFrameEvent, pose: object):
        self.calls.append((intent_id, event, pose))
        return CoverageObservation(True, "accepted", ())


_DETECTOR_CONFIG_SHA256 = "a" * 64


def _mapping() -> ClockMapping:
    return ClockMapping("camera-pts", "relay-monotonic", 0, 10_000, 1000, 15, True)


def _publisher() -> DetectionPublisher:
    return DetectionPublisher(
        DetectionPublisherConfig(
            "session-1",
            "intent-1",
            "intent-1:v1:e7",
            1,
            7,
            "camera-1",
            "camera-serial-1",
            _mapping(),
            ClockMapping("processing-monotonic", "relay-monotonic", 0, 10_000, 1000, 15, True),
        ),
        b"perception-key",
    )


def _frame(frame_id: str = "1", timestamp_s: float = 11) -> dict[str, object]:
    return _publisher().enqueue(
        ProcessedFrameEvent(
            FrameIdentity("camera-1", "intent-1:v1:e7", frame_id, 1),
            timestamp_s,
            timestamp_s + 0.01,
            timestamp_s + 0.01,
            "empty",
            0,
            ("backpack",),
            _DETECTOR_CONFIG_SHA256,
            capture_time_verified=True,
            received_at_s=timestamp_s,
        )
    )


def _ingress(runtime: _Runtime) -> DetectionIngress:
    pin = DetectionSourcePin(
        1,
        "camera-1",
        "camera-serial-1",
        "camera-calibration-1",
        "intent-1",
        "intent-1:v1:e7",
        _mapping(),
    )

    def pose(state: DetectionDroneState, identity: FrameIdentity, capture_ms: int):
        assert state.connection_epoch == 7
        return TrustedCapturePose(
            identity,
            7,
            Pose(1, 2, 1, "level_1"),
            capture_ms,
            capture_ms + 20,
            ControlProvenance(
                "map-1",
                "geometry-1",
                "camera-calibration-1",
                "body-1",
                "camera-pts",
                "relay-monotonic",
                ("tag-1",),
                1,
                15,
                "ready",
                capture_ms,
                0.1,
            ),
        )

    return DetectionIngress(
        DetectionIngressConfig("session-1", {1: pin}),
        runtime,  # type: ignore[arg-type]
        lambda drone_id: DetectionDroneState(drone_id, 7, "intent-1:v1:e7"),
        pose,
    )


def _principal() -> Principal:
    return Principal("perception", None, b"perception-key")


def test_ingress_requires_signed_current_membership_and_relay_owned_pose() -> None:
    runtime = _Runtime([])
    result = _ingress(runtime).consume(_frame(), _principal(), 11_030)

    assert result.accepted
    assert result.reason == "accepted"
    assert len(runtime.calls) == 1
    assert runtime.calls[0][0] == "intent-1"


def test_ingress_rejects_tampered_or_decode_only_processed_frames() -> None:
    tampered = _frame()
    tampered["connection_epoch"] = 8
    runtime = _Runtime([])
    assert _ingress(runtime).consume(tampered, _principal(), 11_030).reason == "invalid_signature"

    unverified = _frame()
    unverified["capture_time_verified"] = False
    unsigned = {key: value for key, value in unverified.items() if key != "signature"}
    unverified["signature"] = sign_event(unsigned, b"perception-key")
    result = _ingress(runtime).consume(unverified, _principal(), 11_030)

    assert result == type(result)(False, "capture_time_unverified")
    assert runtime.calls == []


def test_ingress_rejects_old_processed_capture_after_seen_ids_are_evicted() -> None:
    ingress = _ingress(_Runtime([]))

    ingress.consume(_frame("new", 11.1), _principal(), 11_130)
    result = ingress.consume(_frame("old", 11), _principal(), 11_130)

    assert result.reason == "stale_capture_timestamp"
