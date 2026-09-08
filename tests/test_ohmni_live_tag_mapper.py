from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from perception.ohmni_clock_probe import parse_probe_reply, probe_clock
from perception.ohmni_pts_capture import CapturedFrame
from relay.observation_ingress import ObservationConfiguration, ObservationIngress
from relay.observations import (
    ClockMapping,
    FrameDeclaration,
    FrameRegistry,
    SourceBinding,
    decode_observation,
)
from tools.ohmni_live_tag_mapper import (
    ArchiveConfig,
    ContinuousAcceptedObservationArchive,
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


@pytest.mark.parametrize("membership", ("registered", "ready", "degraded"))
def test_scope_uses_current_active_ground_epoch_from_relay_state(membership: str) -> None:
    scope = scope_from_state(
        {
            "type": "state",
            "session": "live-12",
            "drones": [
                {
                    "drone_id": 12,
                    "node_type": "ground",
                    "membership": membership,
                    "connection_epoch": 17,
                }
            ],
        },
        session="live-12",
        device_id=12,
    )

    assert scope == LiveScope("live-12", 12, 17)


@pytest.mark.parametrize("membership", ("leaving", "disconnected", "unknown"))
def test_scope_rejects_a_noncurrent_ground_epoch(membership: str) -> None:
    state = {
        "type": "state",
        "session": "live-12",
        "drones": [
            {
                "drone_id": 12,
                "node_type": "ground",
                "membership": membership,
                "connection_epoch": 17,
            }
        ],
    }

    with pytest.raises(LiveMapperError, match="no current active epoch"):
        scope_from_state(state, session="live-12", device_id=12)


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
                                    "membership": "ready",
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


def test_publisher_archives_relay_accepted_camera_tag_pose_and_scan_events(tmp_path) -> None:
    from tools.ohmni_live_tag_mapper import AcceptedObservationArchive, ArchiveConfig

    mapper = _mapper()
    scope = LiveScope("live-12", 12, 9)
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

    def accepted(submission, ingest: int) -> dict[str, object]:
        return ingress.accept(submission, now=ingest, producer_role="localization").to_mapping()

    def pose() -> dict[str, object]:
        return {
            "v": 1,
            "type": "observation",
            "event_id": "pose-1",
            "session": "live-12",
            "device_id": 12,
            "connection_epoch": 9,
            "source_id": "ohmni-pose",
            "node_type": "ground",
            "frame": "odom",
            "confidence": 0.8,
            "t_capture": None,
            "t_source_receipt": {"clock_id": "ohmni-monotonic", "unit": "ns", "value": 10},
            "clock_mapping_id": None,
            "payload": {
                "kind": "pose",
                "pose": {
                    "parent_frame": "odom",
                    "child_frame": "body",
                    "x_m": 0.0,
                    "y_m": 0.0,
                    "z_m": 0.0,
                    "qx": 0.0,
                    "qy": 0.0,
                    "qz": 0.0,
                    "qw": 1.0,
                },
            },
            "t_ingest": 10,
        }

    def scan() -> dict[str, object]:
        return {
            "v": 1,
            "type": "observation",
            "event_id": "scan-1",
            "session": "live-12",
            "device_id": 12,
            "connection_epoch": 9,
            "source_id": "ohmni-lidar",
            "node_type": "ground",
            "frame": "lidar",
            "confidence": 0.8,
            "t_capture": None,
            "t_source_receipt": {"clock_id": "ohmni-monotonic", "unit": "ns", "value": 11},
            "clock_mapping_id": None,
            "payload": {
                "kind": "range_scan",
                "sensor_pose": {
                    "parent_frame": "odom",
                    "child_frame": "lidar",
                    "x_m": 0.0,
                    "y_m": 0.0,
                    "z_m": 0.2,
                    "qx": 0.0,
                    "qy": 0.0,
                    "qz": 0.0,
                    "qw": 1.0,
                },
                "angle_min_rad": 0.0,
                "angle_increment_rad": 1.0,
                "range_min_m": 0.1,
                "range_max_m": 8.0,
                "ranges_m": [1.0],
                "mount_id": "ohmni-rplidar",
            },
            "t_ingest": 11,
        }

    class Socket:
        def __init__(self) -> None:
            self.inbound = [
                json.dumps({"type": "auth.accepted"}),
                json.dumps(
                    {
                        "type": "state",
                        "session": "live-12",
                        "drones": [
                            {
                                "drone_id": 12,
                                "node_type": "ground",
                                "membership": "ready",
                                "connection_epoch": 9,
                            }
                        ],
                    }
                ),
            ]
            self.first = True
            self.ingest = 1_090

        async def recv(self) -> str:
            return self.inbound.pop(0)

        async def send(self, message: str) -> None:
            from relay.observations import decode_submission

            event = decode_submission(message)
            if self.first:
                self.first = False
                self.inbound.extend((json.dumps(pose()), json.dumps(scan())))
            self.ingest += 10
            self.inbound.append(json.dumps(accepted(event, self.ingest)))

    archive = AcceptedObservationArchive(
        tmp_path / "archive",
        scope=scope,
        mapper=mapper.config,
        config=ArchiveConfig("ohmni-pose", "ohmni-lidar", "odom", "body", "lidar"),
    )
    assert asyncio.run(publish_observations(Socket(), mapper, (_frame(),), archive=archive)) == 3
    manifest = archive.finish()

    lines = (tmp_path / "archive" / "observations.jsonl").read_bytes().splitlines()
    observations = [decode_observation(line) for line in lines]
    assert manifest["observations"]["count"] == len(observations) == 5
    assert {item.submission.payload["kind"] for item in observations} == {
        "camera_frame",
        "tag_observation",
        "pose",
        "range_scan",
    }
    assert all(item.t_ingest is not None for item in observations)
    assert all(item.submission.connection_epoch == 9 for item in observations)
    assert {
        item.t_ingest for item in observations if item.submission.source_id.startswith("ohmni-live")
    } == {1_100, 1_110, 1_120}


def test_continuous_archive_repeats_one_accepted_pose_and_records_raw_and_unique_counts(
    tmp_path,
) -> None:
    from relay.observations import Observation
    from tools.ohmni_live_tag_mapper import ContinuousAcceptedObservationArchive

    mapper = _mapper()
    scope = LiveScope("live-12", 12, 9)
    archive = ContinuousAcceptedObservationArchive(
        tmp_path / "collection",
        scope=scope,
        mapper=mapper.config,
        config=ArchiveConfig(
            "ohmni-pose",
            "ohmni-lidar",
            "odom",
            "body",
            "lidar",
            max_records=2,
        ),
        max_archives=2,
    )
    pose = {
        "v": 1,
        "type": "observation",
        "event_id": "pose-1",
        "session": "live-12",
        "device_id": 12,
        "connection_epoch": 9,
        "source_id": "ohmni-pose",
        "node_type": "ground",
        "frame": "odom",
        "confidence": 0.8,
        "t_capture": {"clock_id": "ohmni12-boot-monotonic", "unit": "ns", "value": 10},
        "t_source_receipt": {
            "clock_id": "ohmni12-boot-monotonic",
            "unit": "ns",
            "value": 10,
        },
        "clock_mapping_id": "ohmni12-live",
        "payload": {
            "kind": "pose",
            "pose": {
                "parent_frame": "odom",
                "child_frame": "body",
                "x_m": 0.0,
                "y_m": 0.0,
                "z_m": 0.0,
                "qx": 0.0,
                "qy": 0.0,
                "qz": 0.0,
                "qw": 1.0,
            },
        },
        "t_ingest": 10,
    }
    camera = Observation(mapper.observations(scope, _frame())[0], 11).to_mapping()

    assert archive.observe(pose)
    assert archive.observe(camera)
    assert not archive.stopped
    manifest = archive.finish()

    assert manifest["raw_observations"]["count"] == 3
    assert manifest["unique_observations"]["count"] == 2
    assert manifest["handoffs"][0]["event_id"] == "pose-1"
    first = (tmp_path / "collection" / "archive-000" / "observations.jsonl").read_bytes()
    second = (tmp_path / "collection" / "archive-001" / "observations.jsonl").read_bytes()
    assert first.splitlines()[0] == second.splitlines()[0]
    assert (tmp_path / "collection" / "collection.json").is_file()

    now = [0.0]
    idle = ContinuousAcceptedObservationArchive(
        tmp_path / "idle",
        scope=scope,
        mapper=mapper.config,
        config=ArchiveConfig(
            "ohmni-pose", "ohmni-lidar", "odom", "body", "lidar", max_records=3, duration_s=1
        ),
        max_archives=2,
        monotonic=lambda: now[0],
    )
    assert idle.observe(pose)
    now[0] = 1.1
    assert idle.remaining_s == 0
    idle_manifest = idle.finish()
    assert idle_manifest["raw_observations"]["count"] == 1
    assert not (tmp_path / "idle" / "archive-001").exists()


@pytest.mark.parametrize("limit", ("records", "bytes"))
def test_continuous_archive_stops_before_exceeding_its_combined_consumer_limit(
    tmp_path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    from relay.observations import Observation
    from tools import ohmni_live_tag_mapper
    from tools.ohmni_live_tag_mapper import ContinuousAcceptedObservationArchive

    mapper = _mapper()
    scope = LiveScope("live-12", 12, 9)
    pose = {
        "v": 1,
        "type": "observation",
        "event_id": "pose-1",
        "session": "live-12",
        "device_id": 12,
        "connection_epoch": 9,
        "source_id": "ohmni-pose",
        "node_type": "ground",
        "frame": "odom",
        "confidence": 0.8,
        "t_capture": {"clock_id": "ohmni12-boot-monotonic", "unit": "ns", "value": 10},
        "t_source_receipt": {
            "clock_id": "ohmni12-boot-monotonic",
            "unit": "ns",
            "value": 10,
        },
        "clock_mapping_id": "ohmni12-live",
        "payload": {
            "kind": "pose",
            "pose": {
                "parent_frame": "odom",
                "child_frame": "body",
                "x_m": 0.0,
                "y_m": 0.0,
                "z_m": 0.0,
                "qx": 0.0,
                "qy": 0.0,
                "qz": 0.0,
                "qw": 1.0,
            },
        },
        "t_ingest": 10,
    }
    camera = Observation(mapper.observations(scope, _frame())[0], 11).to_mapping()
    second_camera = {**camera, "event_id": "camera-2", "t_ingest": 12}
    pose_bytes = len(Observation.parse(pose).encode()) + 1
    camera_bytes = len(Observation.parse(camera).encode()) + 1
    monkeypatch.setattr(
        ohmni_live_tag_mapper,
        "MAX_CONTINUOUS_ARCHIVE_RECORDS",
        3 if limit == "records" else 100,
    )
    monkeypatch.setattr(
        ohmni_live_tag_mapper,
        "MAX_CONTINUOUS_ARCHIVE_BYTES",
        1_000_000 if limit == "records" else pose_bytes * 2 + camera_bytes,
    )
    archive = ContinuousAcceptedObservationArchive(
        tmp_path / limit,
        scope=scope,
        mapper=mapper.config,
        config=ArchiveConfig("ohmni-pose", "ohmni-lidar", "odom", "body", "lidar", max_records=2),
        max_archives=3,
    )

    assert archive.observe(pose)
    assert archive.observe(camera)
    assert not archive.stopped
    assert not archive.observe(second_camera)
    assert archive.stopped
    manifest = archive.finish()

    assert manifest["raw_observations"]["count"] == 3
    assert manifest["unique_observations"]["count"] == 2


def test_continuous_archive_requires_room_after_a_handoff(tmp_path) -> None:
    with pytest.raises(LiveMapperError, match="leave room"):
        ContinuousAcceptedObservationArchive(
            tmp_path / "collection",
            scope=LiveScope("live-12", 12, 9),
            mapper=_mapper().config,
            config=ArchiveConfig(
                "ohmni-pose", "ohmni-lidar", "odom", "body", "lidar", max_records=1
            ),
            max_archives=2,
        )


def test_continuous_collection_checks_the_relay_installed_capture_pose_registration(
    tmp_path,
) -> None:
    from tools.ohmni_live_tag_mapper import _capture_pose_registration

    registration = tmp_path / "observations.json"
    registration.write_text(
        json.dumps(
            {
                "bindings": [
                    {
                        "session": "live-12",
                        "device_id": 12,
                        "connection_epoch": 9,
                        "source_id": "ohmni-capture-pose",
                        "node_type": "ground",
                        "allowed_frames": ["odom", "body"],
                        "allowed_payload_kinds": ["pose"],
                        "allowed_clock_mapping_ids": ["ohmni12-live"],
                        "producer_role": "adapter",
                    }
                ],
                "frames": [
                    {
                        "frame_id": "odom",
                        "kind": "odom",
                        "axis_convention": "right_handed_z_up",
                        "unit": "m",
                        "session": "live-12",
                        "device_id": 12,
                        "connection_epoch": 9,
                        "source_id": "ohmni-capture-pose",
                    },
                    {
                        "frame_id": "body",
                        "kind": "body",
                        "axis_convention": "forward_left_up",
                        "unit": "m",
                        "session": "live-12",
                        "device_id": 12,
                        "connection_epoch": 9,
                        "source_id": "ohmni-capture-pose",
                    },
                ],
                "clock_mappings": [
                    {
                        "mapping_id": "ohmni12-live",
                        "source_clock_id": "ohmni12-boot-monotonic",
                        "source_unit": "ns",
                        "source_reference": 1_000,
                        "relay_reference_ms": 2_000,
                        "relay_ms_numerator": 1,
                        "source_units_denominator": 1_000_000,
                        "max_error_ms": 10,
                    }
                ],
                "minimum_interval_ms": 10,
            }
        )
    )
    registration_record = _capture_pose_registration(
        registration,
        LiveScope("live-12", 12, 9),
        _mapper().config,
        ArchiveConfig("ohmni-capture-pose", "ohmni-lidar", "odom", "body", "lidar"),
    )

    assert registration_record["mapping_id"] == "ohmni12-live"
    assert registration_record["sha256"] == hashlib.sha256(registration.read_bytes()).hexdigest()


def test_publisher_finishes_a_valid_archive_at_the_record_bound(tmp_path) -> None:
    from relay.observations import Observation, decode_submission
    from tools.ohmni_live_tag_mapper import AcceptedObservationArchive, ArchiveConfig

    scope = LiveScope("live-12", 12, 9)

    class Socket:
        def __init__(self) -> None:
            self.inbound = [
                json.dumps({"type": "auth.accepted"}),
                json.dumps(
                    {
                        "type": "state",
                        "session": "live-12",
                        "drones": [
                            {
                                "drone_id": 12,
                                "node_type": "ground",
                                "membership": "ready",
                                "connection_epoch": 9,
                            }
                        ],
                    }
                ),
            ]
            self.sent: list[dict[str, object]] = []

        async def recv(self) -> str:
            return self.inbound.pop(0)

        async def send(self, message: str) -> None:
            submission = decode_submission(message)
            self.sent.append(submission.to_mapping())
            self.inbound.append(json.dumps(Observation(submission, 12).to_mapping()))

    mapper = _mapper()
    archive = AcceptedObservationArchive(
        tmp_path / "archive",
        scope=scope,
        mapper=mapper.config,
        config=ArchiveConfig("ohmni-pose", "ohmni-lidar", "odom", "body", "lidar", max_records=1),
    )
    socket = Socket()
    assert asyncio.run(publish_observations(socket, mapper, (_frame(),), archive=archive)) == 1
    manifest = archive.finish()

    assert manifest["observations"]["stop_reason"] == "max_records"
    assert manifest["observations"]["count"] == 1
    assert len(socket.sent) == 1
    assert (tmp_path / "archive" / "manifest.json").is_file()


def test_publisher_removes_an_archive_after_a_bad_canonical_observation(tmp_path) -> None:
    from tools.ohmni_live_tag_mapper import AcceptedObservationArchive, ArchiveConfig

    class Socket:
        def __init__(self) -> None:
            self.inbound = [
                json.dumps({"type": "auth.accepted"}),
                json.dumps(
                    {
                        "type": "state",
                        "session": "live-12",
                        "drones": [
                            {
                                "drone_id": 12,
                                "node_type": "ground",
                                "membership": "ready",
                                "connection_epoch": 9,
                            }
                        ],
                    }
                ),
            ]

        async def recv(self) -> str:
            return self.inbound.pop(0)

        async def send(self, _message: str) -> None:
            self.inbound.append(json.dumps({"type": "observation"}))

    archive = AcceptedObservationArchive(
        tmp_path / "archive",
        scope=LiveScope("live-12", 12, 9),
        mapper=_mapper().config,
        config=ArchiveConfig("ohmni-pose", "ohmni-lidar", "odom", "body", "lidar"),
    )
    with pytest.raises(LiveMapperError, match="invalid accepted observation"):
        asyncio.run(publish_observations(Socket(), _mapper(), (_frame(),), archive=archive))
    assert not (tmp_path / "archive").exists()


def test_archive_finishes_at_the_byte_bound_without_exceeding_it(tmp_path) -> None:
    from relay.observations import Observation
    from tools.ohmni_live_tag_mapper import (
        MANIFEST_RESERVE_BYTES,
        AcceptedObservationArchive,
        ArchiveConfig,
    )

    mapper = _mapper()
    scope = LiveScope("live-12", 12, 9)
    archive = AcceptedObservationArchive(
        tmp_path / "archive",
        scope=scope,
        mapper=mapper.config,
        config=ArchiveConfig(
            "ohmni-pose",
            "ohmni-lidar",
            "odom",
            "body",
            "lidar",
            max_bytes=MANIFEST_RESERVE_BYTES,
        ),
    )
    camera = mapper.observations(scope, _frame())[0]
    assert not archive.observe(Observation(camera, 1_100).to_mapping())
    manifest = archive.finish()

    assert manifest["observations"]["stop_reason"] == "max_bytes"
    assert manifest["observations"]["count"] == 0
    assert (
        sum(path.stat().st_size for path in (tmp_path / "archive").iterdir())
        <= MANIFEST_RESERVE_BYTES
    )


def test_publisher_stops_a_silent_frame_reader_at_the_archive_deadline(tmp_path) -> None:
    from tools.ohmni_live_tag_mapper import (
        AcceptedObservationArchive,
        ArchiveConfig,
        _publish_reader,
    )

    release = threading.Event()
    closed: list[bool] = []

    class Frames:
        def __iter__(self):
            return self

        def __next__(self) -> CapturedFrame:
            release.wait()
            raise StopIteration

    archive = AcceptedObservationArchive(
        tmp_path / "archive",
        scope=LiveScope("live-12", 12, 9),
        mapper=_mapper().config,
        config=ArchiveConfig("ohmni-pose", "ohmni-lidar", "odom", "body", "lidar", duration_s=0.01),
    )

    async def exercise() -> int:
        try:
            return await _publish_reader(
                object(),
                _mapper(),
                LiveScope("live-12", 12, 9),
                Frames(),
                0,
                1,
                archive,
                close_frames=lambda: closed.append(True),
            )
        finally:
            release.set()

    started = time.monotonic()
    assert asyncio.run(exercise()) == 0
    assert time.monotonic() - started < 0.5
    assert closed == [True]
    assert archive.finish()["observations"]["stop_reason"] == "duration"


def test_frame_source_shutdown_unblocks_a_buffered_socket_reader() -> None:
    from tools.ohmni_live_tag_mapper import _close_frame_source

    source, peer = socket.socketpair()
    stream = source.makefile("rb")
    reading = threading.Event()
    finished = threading.Event()

    def read_one() -> None:
        reading.set()
        try:
            stream.read(1)
        finally:
            finished.set()

    reader = threading.Thread(target=read_one)
    reader.start()
    try:
        assert reading.wait(0.5)
        _close_frame_source(source, stream)
        reader.join(0.5)
        assert finished.is_set()
        assert not reader.is_alive()
    finally:
        peer.close()


def test_publisher_stops_a_silent_relay_at_the_archive_deadline(tmp_path) -> None:
    from tools.ohmni_live_tag_mapper import AcceptedObservationArchive, ArchiveConfig

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
                                    "membership": "ready",
                                    "connection_epoch": 9,
                                }
                            ],
                        }
                    ),
                )
            )

        async def recv(self) -> str:
            try:
                return next(self.inbound)
            except StopIteration:
                await asyncio.Future()
                raise AssertionError("unreachable") from None

        async def send(self, _message: str) -> None:
            return None

    archive = AcceptedObservationArchive(
        tmp_path / "archive",
        scope=LiveScope("live-12", 12, 9),
        mapper=_mapper().config,
        config=ArchiveConfig("ohmni-pose", "ohmni-lidar", "odom", "body", "lidar", duration_s=0.01),
    )
    started = time.monotonic()
    assert asyncio.run(publish_observations(Socket(), _mapper(), (_frame(),), archive=archive)) == 0
    assert time.monotonic() - started < 0.5
    assert archive.finish()["observations"]["stop_reason"] == "duration"


def test_confirmation_stops_when_an_unrelated_archive_event_reaches_its_bound(tmp_path) -> None:
    from relay.observations import Observation
    from tools.ohmni_live_tag_mapper import (
        AcceptedObservationArchive,
        ArchiveConfig,
        _confirm_submission,
    )

    mapper = _mapper()
    scope = LiveScope("live-12", 12, 9)
    unrelated, event, *_ = mapper.observations(scope, _frame())
    archive = AcceptedObservationArchive(
        tmp_path / "archive",
        scope=scope,
        mapper=mapper.config,
        config=ArchiveConfig("ohmni-pose", "ohmni-lidar", "odom", "body", "lidar", max_records=1),
    )

    class Socket:
        sent = False

        async def recv(self) -> str:
            if not self.sent:
                self.sent = True
                return json.dumps(Observation(unrelated, 1_100).to_mapping())
            await asyncio.Future()
            raise AssertionError("unreachable") from None

    started = time.monotonic()
    assert asyncio.run(_confirm_submission(Socket(), scope, event, 1, archive)) == (False, True)
    assert time.monotonic() - started < 0.5
    assert archive.finish()["observations"]["stop_reason"] == "max_records"


def test_archive_drain_stops_after_its_silent_receive_deadline(tmp_path) -> None:
    from tools.ohmni_live_tag_mapper import (
        AcceptedObservationArchive,
        ArchiveConfig,
        _drain_archive,
    )

    class SilentSocket:
        async def recv(self) -> str:
            await asyncio.Future()
            raise AssertionError("unreachable")

    archive = AcceptedObservationArchive(
        tmp_path / "archive",
        scope=LiveScope("live-12", 12, 9),
        mapper=_mapper().config,
        config=ArchiveConfig("ohmni-pose", "ohmni-lidar", "odom", "body", "lidar"),
    )
    started = time.monotonic()
    asyncio.run(_drain_archive(SilentSocket(), LiveScope("live-12", 12, 9), archive, 0.01))
    assert time.monotonic() - started < 0.5
    assert archive.finish()["observations"]["stop_reason"] == "input_exhausted"


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
                                    "membership": "ready",
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
    assert len(delays) == 1
    assert 0 < delays[0] <= 0.01


def test_publisher_spaces_tag_events_across_consecutive_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import ohmni_live_tag_mapper

    class SingleTagDetector(_Detector):
        def detect(self, image: np.ndarray) -> list[dict[str, object]]:
            return super().detect(image)[:1]

    mapper = LiveTagMapper(
        _mapper().config,
        SingleTagDetector(),
        receipt_time_ns=lambda: 1_100_000_000,
        event_ids=iter(("camera-one", "tag-one", "camera-two", "tag-two")).__next__,
    )

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
                                    "membership": "ready",
                                    "connection_epoch": 17,
                                }
                            ],
                        }
                    ),
                    json.dumps({"type": "observation", "event_id": "camera:camera-one"}),
                    json.dumps({"type": "observation", "event_id": "tag:tag-one"}),
                    json.dumps({"type": "observation", "event_id": "camera:camera-two"}),
                    json.dumps({"type": "observation", "event_id": "tag:tag-two"}),
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
            publish_observations(Socket(), mapper, (_frame(), _frame()), tag_submit_interval_ms=10)
        )
        == 4
    )
    assert len(delays) == 1
    assert 0 < delays[0] <= 0.01


def test_relay_state_chatter_cannot_extend_submission_confirmation_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import ohmni_live_tag_mapper
    from tools.ohmni_live_tag_mapper import _confirm_submission

    state = json.dumps(
        {
            "type": "state",
            "session": "live-12",
            "drones": [
                {
                    "drone_id": 12,
                    "node_type": "ground",
                    "membership": "ready",
                    "connection_epoch": 9,
                }
            ],
        }
    )

    class Chatter:
        async def recv(self) -> str:
            await asyncio.sleep(0)
            return state

    event = _mapper().observations(LiveScope("live-12", 12, 9), _frame())[0]
    monkeypatch.setattr(ohmni_live_tag_mapper, "_receive_timeout", lambda _value: 0.001)
    with pytest.raises(LiveMapperError, match="timed out waiting"):
        asyncio.run(_confirm_submission(Chatter(), LiveScope("live-12", 12, 9), event, 1))


def test_silent_relay_times_out_during_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import ohmni_live_tag_mapper
    from tools.ohmni_live_tag_mapper import _authenticated_scope

    class Silent:
        async def recv(self) -> str:
            await asyncio.Future()
            raise AssertionError("unreachable")

    monkeypatch.setattr(ohmni_live_tag_mapper, "_receive_timeout", lambda _value: 0.001)
    with pytest.raises(LiveMapperError, match="timed out waiting"):
        asyncio.run(_authenticated_scope(Silent(), _mapper(), 1))


def test_main_closes_the_sidecar_and_removes_the_reverse_tunnel_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from tools import ohmni_live_tag_mapper

    class Detector:
        camera_serial = "ohmni-live"
        width = 16
        height = 12

    class Source:
        closed = False

        def makefile(self, _mode: str):
            raise RuntimeError("sidecar stream failed")

        def close(self) -> None:
            self.closed = True

    source = Source()
    calls: list[tuple[str, int]] = []
    args = SimpleNamespace(
        calibration="unused",
        calibration_sha256="a" * 64,
        camera_serial="ohmni-live",
        tag_sizes={7: 0.16},
        session="live-12",
        device_id=12,
        camera_source_id="ohmni-live-camera",
        tag_source_id="ohmni-live-tag",
        camera_frame="camera",
        clock_id="robot-boot",
        clock_mapping_id="robot-live",
        maximum_capture_lag_ms=1_000,
        confidence=0.8,
        covariance=(0.01, 0.0, 0.0, 0.0, 0.01, 0.0, 0.0, 0.0, 0.02),
        adb="adb",
        adb_serial="robot:5555",
        pts_port=18555,
        sidecar_connect_timeout_s=5,
        tag_submit_interval_ms=10,
        relay_receive_timeout_s=5,
    )
    monkeypatch.setattr(ohmni_live_tag_mapper, "read_calibration", lambda *_args: {})
    monkeypatch.setattr(
        ohmni_live_tag_mapper, "CameraTagDetector", lambda *_args, **_kwargs: Detector()
    )
    monkeypatch.setattr(
        ohmni_live_tag_mapper,
        "_qualify_clock",
        lambda _args: SimpleNamespace(robot_time_ns=lambda value: value),
    )
    monkeypatch.setattr(
        ohmni_live_tag_mapper,
        "_adb_reverse",
        lambda _adb, _serial, port: calls.append(("setup", port)),
    )
    monkeypatch.setattr(
        ohmni_live_tag_mapper,
        "_remove_adb_reverse",
        lambda _adb, _serial, port: calls.append(("remove", port)),
    )

    async def serve(_port: int, _timeout_s: float) -> Source:
        return source

    monkeypatch.setattr(ohmni_live_tag_mapper, "_serve_one", serve)
    with pytest.raises(RuntimeError, match="sidecar stream failed"):
        asyncio.run(ohmni_live_tag_mapper._main_async(args))
    assert source.closed is True
    assert calls == [("setup", 18555), ("remove", 18555)]


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


def test_adb_clock_query_executes_the_robot_monotonic_protocol_through_adb(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tools import ohmni_live_tag_mapper

    loader = tmp_path / "loader"
    loader.write_text('#!/bin/sh\nexec "$@"\n')
    loader.chmod(0o755)
    monkeypatch.setattr(ohmni_live_tag_mapper, "OHMNI_LOADER", str(loader))
    monkeypatch.setattr(ohmni_live_tag_mapper, "OHMNI_PYTHON", sys.executable)
    local_run = subprocess.run
    replies: list[str] = []

    def adb_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[:4] == ["adb", "-s", "robot:5555", "shell"]
        assert kwargs["timeout"] == ohmni_live_tag_mapper.ADB_CLOCK_QUERY_TIMEOUT_S
        completed = local_run(["sh", "-c", command[4]], check=False, capture_output=True, text=True)
        replies.append(completed.stdout)
        return completed

    monkeypatch.setattr(ohmni_live_tag_mapper.subprocess, "run", adb_run)
    sample = probe_clock(
        lambda: ohmni_live_tag_mapper._adb_clock_query("adb", "robot:5555"),
        monotonic_ns=lambda: time.clock_gettime_ns(time.CLOCK_MONOTONIC),
    )
    boot_id, timestamp_ns, resolution_ns = parse_probe_reply(replies[0])

    assert boot_id == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    assert timestamp_ns == sample.robot_monotonic_ns
    assert resolution_ns == 1
    assert sample.host_before_ns <= sample.robot_monotonic_ns <= sample.host_after_ns


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


def test_sidecar_socket_handoff_preserves_bytes_sent_at_connect() -> None:
    from tools.ohmni_live_tag_mapper import _serve_one

    payload = b"nut-sidecar-preamble"

    async def receive() -> bytes:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        accepting = asyncio.create_task(_serve_one(port, 1))
        await asyncio.sleep(0)
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(payload)
        await writer.drain()
        source = await accepting
        try:
            assert source.getblocking() is True
            return source.recv(len(payload))
        finally:
            source.close()
            writer.close()
            await writer.wait_closed()

    assert asyncio.run(receive()) == payload
