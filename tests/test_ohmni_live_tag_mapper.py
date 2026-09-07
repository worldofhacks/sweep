from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from perception.ohmni_pts_capture import CapturedFrame
from relay.observation_ingress import ObservationConfiguration, ObservationIngress
from relay.observations import ClockMapping, FrameDeclaration, FrameRegistry, SourceBinding
from tools.ohmni_live_tag_mapper import (
    LiveMapperError,
    LiveScope,
    LiveTagMapper,
    MapperConfig,
    publish_observations,
    scope_from_state,
)


class _Detector:
    camera_serial = "ohmni-head-12"
    width = 16
    height = 12

    def detect(self, _image: np.ndarray) -> list[dict[str, object]]:
        return [
            {
                "tag_id": 7,
                "pose_accepted": True,
                "T_camera_tag": np.array(
                    [
                        [1.0, 0.0, 0.0, 1.0],
                        [0.0, 1.0, 0.0, 2.0],
                        [0.0, 0.0, 1.0, 3.0],
                        [0.0, 0.0, 0.0, 1.0],
                    ]
                ),
                "reason": "pose",
                "size_m": 0.16,
                "corners_px": [[1.0, 1.0], [4.0, 1.0], [4.0, 4.0], [1.0, 4.0]],
                "pixel_frame": "rectified_camera",
                "reprojection_rms_px": 0.2,
            },
            {
                "tag_id": 8,
                "pose_accepted": True,
                "T_camera_tag": np.array(
                    [
                        [1.0, 0.0, 0.0, 4.0],
                        [0.0, 1.0, 0.0, 5.0],
                        [0.0, 0.0, 1.0, 6.0],
                        [0.0, 0.0, 0.0, 1.0],
                    ]
                ),
                "reason": "pose",
                "size_m": 0.16,
                "corners_px": [[6.0, 1.0], [9.0, 1.0], [9.0, 4.0], [6.0, 4.0]],
                "pixel_frame": "rectified_camera",
                "reprojection_rms_px": 0.2,
            },
        ]


def _mapper(*, receipt_ns: int = 1_100_000_000) -> LiveTagMapper:
    return LiveTagMapper(
        MapperConfig(
            session="live-12",
            device_id=12,
            camera_source_id="ohmni-live-camera",
            tag_source_id="ohmni-live-tag",
            camera_frame="camera",
            camera_serial="ohmni-head-12",
            calibration_id="sha256:" + "a" * 64,
            clock_id="ohmni12-boot-monotonic",
            clock_mapping_id="ohmni12-live",
            maximum_capture_lag_ns=5_000_000_000,
            confidence=0.8,
            covariance_m2=(0.01, 0.0, 0.0, 0.0, 0.01, 0.0, 0.0, 0.0, 0.02),
        ),
        _Detector(),  # type: ignore[arg-type]
        receipt_time_ns=lambda: receipt_ns,
        event_ids=iter(("camera-event", "tag-seven-event", "tag-eight-event")).__next__,
    )


def _frame() -> CapturedFrame:
    return CapturedFrame(np.zeros((12, 16, 3), dtype=np.uint8), 1_000_000_000)


def test_mapper_emits_capture_timestamped_camera_and_tag_events_accepted_by_live_ingress() -> None:
    mapper = _mapper()
    scope = LiveScope("live-12", 12, 9)
    camera, *tags = mapper.observations(scope, _frame())

    assert camera.t_capture is not None
    assert camera.t_capture.value == 1_000_000_000
    assert [tag.t_capture for tag in tags] == [camera.t_capture, camera.t_capture]
    assert [tag.payload["tag_id"] for tag in tags] == [7, 8]
    assert all(tag.source_id == "ohmni-live-tag" for tag in tags)
    assert all(tag.payload["tag_pose"] is not None for tag in tags)

    mapping = ClockMapping(
        "ohmni12-live",
        "ohmni12-boot-monotonic",
        "ns",
        1_000_000_000,
        1_000,
        1,
        1_000_000,
        0,
    )
    camera_scope = ("live-12", 12, 9, "ohmni-live-camera")
    tag_scope = ("live-12", 12, 9, "ohmni-live-tag")
    ingress = ObservationIngress(
        ObservationConfiguration(
            bindings=(
                SourceBinding(
                    *camera_scope,
                    "ground",
                    ("camera",),
                    ("camera_frame",),
                    allowed_clock_mapping_ids=(mapping.mapping_id,),
                    producer_role="localization",
                ),
                SourceBinding(
                    *tag_scope,
                    "ground",
                    ("camera", "tag:7", "tag:8"),
                    ("tag_observation",),
                    allowed_clock_mapping_ids=(mapping.mapping_id,),
                    producer_role="localization",
                ),
            ),
            frames=FrameRegistry(
                (
                    FrameDeclaration("camera", "camera", "right_down_forward", "m", *camera_scope),
                    FrameDeclaration("camera", "camera", "right_down_forward", "m", *tag_scope),
                    FrameDeclaration("tag:7", "tag", "right_up_outward", "m", *tag_scope),
                    FrameDeclaration("tag:8", "tag", "right_up_outward", "m", *tag_scope),
                )
            ),
            clock_mappings=(mapping,),
        ),
        "live-12",
        0,
    )

    admitted_camera = ingress.accept(camera, now=1_100, producer_role="localization")
    admitted_tags = [
        ingress.accept(tag, now=1_100 + index * 10, producer_role="localization")
        for index, tag in enumerate(tags)
    ]

    assert admitted_camera.submission.payload["kind"] == "camera_frame"
    assert [item.submission.payload["tag_id"] for item in admitted_tags] == [7, 8]


def test_mapper_rejects_a_receipt_that_precedes_the_actual_v4l2_pts() -> None:
    with pytest.raises(LiveMapperError, match="precedes"):
        _mapper(receipt_ns=999_999_999).observations(LiveScope("live-12", 12, 9), _frame())


def test_scope_uses_current_ground_epoch_from_relay_state() -> None:
    scope = scope_from_state(
        {
            "type": "state",
            "session": "live-12",
            "drones": [
                {
                    "drone_id": 12,
                    "node_type": "ground",
                    "membership": "joined",
                    "connection_epoch": 17,
                }
            ],
        },
        session="live-12",
        device_id=12,
    )

    assert scope == LiveScope("live-12", 12, 17)


def test_publisher_derives_scope_before_sending_canonical_events() -> None:
    class Socket:
        def __init__(self) -> None:
            self.inbound = iter(
                (
                    json.dumps({"type": "auth.accepted"}),
                    json.dumps(
                        {
                            "type": "state",
                            "session": "live-12",
                            "drones": [
                                {
                                    "drone_id": 12,
                                    "node_type": "ground",
                                    "membership": "joined",
                                    "connection_epoch": 17,
                                }
                            ],
                        }
                    ),
                    json.dumps({"type": "observation", "event_id": "camera:camera-event"}),
                    json.dumps({"type": "observation", "event_id": "tag:tag-seven-event"}),
                    json.dumps({"type": "observation", "event_id": "tag:tag-eight-event"}),
                )
            )
            self.sent: list[dict[str, object]] = []

        async def recv(self) -> str:
            return next(self.inbound)

        async def send(self, message: str) -> None:
            self.sent.append(json.loads(message))

    relay = Socket()
    count = asyncio.run(publish_observations(relay, _mapper(), (_frame(),)))

    assert count == 3
    assert {item["source_id"] for item in relay.sent} == {"ohmni-live-camera", "ohmni-live-tag"}
    assert {item["connection_epoch"] for item in relay.sent} == {17}


def test_publisher_spaces_multiple_tag_events_from_one_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import ohmni_live_tag_mapper

    class Socket:
        def __init__(self) -> None:
            self.inbound = iter(
                (
                    json.dumps({"type": "auth.accepted"}),
                    json.dumps(
                        {
                            "type": "state",
                            "session": "live-12",
                            "drones": [
                                {
                                    "drone_id": 12,
                                    "node_type": "ground",
                                    "membership": "joined",
                                    "connection_epoch": 17,
                                }
                            ],
                        }
                    ),
                    json.dumps({"type": "observation", "event_id": "camera:camera-event"}),
                    json.dumps({"type": "observation", "event_id": "tag:tag-seven-event"}),
                    json.dumps({"type": "observation", "event_id": "tag:tag-eight-event"}),
                )
            )

        async def recv(self) -> str:
            return next(self.inbound)

        async def send(self, _message: str) -> None:
            return None

    delays: list[float] = []

    async def wait(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(ohmni_live_tag_mapper.asyncio, "sleep", wait)
    assert (
        asyncio.run(
            publish_observations(Socket(), _mapper(), (_frame(),), tag_submit_interval_ms=10)
        )
        == 3
    )
    assert delays == [0.01]


def test_clock_qualification_requires_the_pinned_robot_boot_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from tools import ohmni_live_tag_mapper

    ticks = iter((1_000_000_000, 1_002_000_000))
    monkeypatch.setattr(ohmni_live_tag_mapper.time, "monotonic_ns", lambda: next(ticks))
    monkeypatch.setattr(
        ohmni_live_tag_mapper,
        "_adb_clock_query",
        lambda _adb, _serial: "12345678-1234-1234-1234-123456789abc\n39.82\n",
    )
    args = SimpleNamespace(
        adb="adb",
        adb_serial="10.10.0.74:5555",
        clock_probes=1,
        maximum_clock_error_ms=10,
        boot_id="12345678-1234-1234-1234-123456789abc",
    )

    mapping = ohmni_live_tag_mapper._qualify_clock(args)

    assert mapping.boot_id == args.boot_id


def test_mapper_sets_up_and_removes_the_robot_to_host_sidecar_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from tools import ohmni_live_tag_mapper

    calls: list[list[str]] = []
    monkeypatch.setattr(
        ohmni_live_tag_mapper.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or SimpleNamespace(returncode=0),
    )

    ohmni_live_tag_mapper._adb_reverse("adb", "robot:5555", 18555)
    ohmni_live_tag_mapper._remove_adb_reverse("adb", "robot:5555", 18555)

    assert calls == [
        ["adb", "-s", "robot:5555", "reverse", "tcp:18555", "tcp:18555"],
        ["adb", "-s", "robot:5555", "reverse", "--remove", "tcp:18555"],
    ]


def test_mapper_refuses_to_wait_indefinitely_for_the_robot_sidecar() -> None:
    from tools.ohmni_live_tag_mapper import _serve_one

    with pytest.raises(LiveMapperError, match="timed out waiting"):
        asyncio.run(_serve_one(0, 0.001))
