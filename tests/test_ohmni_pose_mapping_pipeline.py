from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import numpy as np

from adapters.ohmni import runtime as runtime_module
from adapters.ohmni.device import Config, OhmniDevice
from adapters.ohmni.paired_encoder import EncoderPair
from adapters.ohmni.runtime import GroundRuntimeConfig, OhmniRuntime
from perception.ohmni_pts_capture import CapturedFrame
from relay.observation_ingress import ObservationConfiguration, ObservationIngress
from relay.observations import (
    ClockMapping,
    FrameDeclaration,
    FrameRegistry,
    Observation,
    SourceBinding,
    decode_submission,
)
from tools.ohmni_live_tag_mapper import LiveScope, LiveTagMapper, MapperConfig
from tools.ohmni_tag_candidate_fusion import _calibration, _mount, _request, fuse_observations

SESSION = "offline-ohmni-pose-mapping"
DEVICE_ID = 11
EPOCH = 1
CLOCK_ID = "ohmni-monotonic"
MAPPING_ID = "offline-nodehrtime-to-relay"
POSE_SOURCE = "ohmni-pose"
CAMERA_SOURCE = "ohmni-live-camera"
TAG_SOURCE = "ohmni-live-tag"
CALIBRATION_BYTES = b'{"fixture":"measured-simulation-v1"}\n'
CALIBRATION_SHA256 = hashlib.sha256(CALIBRATION_BYTES).hexdigest()


class _Shell:
    def close(self) -> None:
        pass


class _Detector:
    camera_serial = "offline-ohmni-head"
    width = 640
    height = 480

    def detect(self, _image: np.ndarray) -> list[dict[str, object]]:
        return [
            {
                "tag_id": 7,
                "pose_accepted": True,
                "T_camera_tag": np.array(
                    (
                        (1.0, 0.0, 0.0, 1.0),
                        (0.0, -1.0, 0.0, 0.0),
                        (0.0, 0.0, -1.0, 1.0),
                        (0.0, 0.0, 0.0, 1.0),
                    )
                ),
                "reason": "pose",
                "size_m": 0.16,
                "corners_px": [[100.0, 100.0], [140.0, 100.0], [140.0, 140.0], [100.0, 140.0]],
                "pixel_frame": "rectified_camera",
                "reprojection_rms_px": 0.2,
            }
        ]


def _calibration_document() -> dict[str, object]:
    return {
        "schema_version": 2,
        "model": "fisheye",
        "status": "offline",
        "evidence_kind": "recorded_live",
        "camera_serial": "offline-ohmni-head",
        "pipeline": {"resolution_px": [640, 480]},
        "checkerboard": {"inner_corners": [9, 6], "square_size_m": 0.02},
        "image_size_px": [640, 480],
        "camera_matrix": [[300.0, 0.0, 320.0], [0.0, 300.0, 240.0], [0.0, 0.0, 1.0]],
        "distortion_coefficients": [0.0, 0.0, 0.0, 0.0],
        "rms_reprojection_error_px": 0.2,
        "accepted_image_count": 20,
        "image_sha256": {f"frame-{index}": f"{index:064x}" for index in range(20)},
        "quality": {
            "accepted_image_count": 20,
            "minimum_accepted_image_count": 20,
            "pose_constraint_ratio": 0.01,
            "minimum_pose_constraint_ratio": 0.005,
            "rms_reprojection_error_px": 0.2,
            "maximum_rms_reprojection_error_px": 0.5,
            "opencv_check_cond": True,
        },
    }


def _mount_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "ohmni_camera_mount",
        "measurement_kind": "measured_rigid_mount",
        "camera_serial": "offline-ohmni-head",
        "body_frame": "body",
        "camera_frame": "camera",
        "T_body_camera": [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
    }


def _runtime_config(*, qualified: bool) -> GroundRuntimeConfig:
    common = {
        "relay_url": "ws://127.0.0.1:1",
        "session": SESSION,
        "device_id": DEVICE_ID,
        "token": "offline-pose-mapping-key",
        "adapter_id": "offline-ohmni",
        "source_clock_id": CLOCK_ID,
    }
    if qualified:
        common.update(
            pose_clock_mapping_id=MAPPING_ID,
            pose_clock_boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            maximum_pose_sample_skew_ns=5_000_000,
        )
    return GroundRuntimeConfig(**common)


def _configuration(first_capture_ns: int) -> ObservationConfiguration:
    scopes = {
        "pose": (SESSION, DEVICE_ID, EPOCH, POSE_SOURCE),
        "camera": (SESSION, DEVICE_ID, EPOCH, CAMERA_SOURCE),
        "tag": (SESSION, DEVICE_ID, EPOCH, TAG_SOURCE),
    }
    mapping = ClockMapping(
        MAPPING_ID,
        CLOCK_ID,
        "ns",
        first_capture_ns,
        10_000,
        1,
        1_000_000,
        1,
    )
    return ObservationConfiguration(
        bindings=(
            SourceBinding(
                *scopes["pose"],
                "ground",
                ("odom", "body"),
                ("pose",),
                allowed_clock_mapping_ids=(MAPPING_ID,),
                producer_role="adapter",
            ),
            SourceBinding(
                *scopes["camera"],
                "ground",
                ("camera",),
                ("camera_frame",),
                allowed_clock_mapping_ids=(MAPPING_ID,),
                producer_role="localization",
            ),
            SourceBinding(
                *scopes["tag"],
                "ground",
                ("camera", "tag:7"),
                ("tag_observation",),
                allowed_clock_mapping_ids=(MAPPING_ID,),
                producer_role="localization",
            ),
        ),
        frames=FrameRegistry(
            (
                FrameDeclaration("odom", "odom", "right_handed_z_up", "m", *scopes["pose"]),
                FrameDeclaration("body", "body", "forward_left_up", "m", *scopes["pose"]),
                FrameDeclaration("camera", "camera", "right_down_forward", "m", *scopes["camera"]),
                FrameDeclaration("camera", "camera", "right_down_forward", "m", *scopes["tag"]),
                FrameDeclaration("tag:7", "tag", "right_up_outward", "m", *scopes["tag"]),
            )
        ),
        clock_mappings=(mapping,),
    )


def _pose_events(
    monkeypatch, device: OhmniDevice, samples: tuple[int, int], *, qualified: bool
) -> list[dict[str, object]]:
    receipt = iter(sample + 1_000_000 for sample in samples)
    monkeypatch.setattr(runtime_module.time, "monotonic_ns", lambda: next(receipt))
    node = OhmniRuntime(_runtime_config(qualified=qualified), device)
    node._epoch = EPOCH
    events = []
    for sample in samples:
        node._outbound = asyncio.Queue()
        device.odometry._update_paired_sample(
            EncoderPair(sample, 100, 100, sample - 1_000_000, sample)
        )
        node._publish_observations()
        events.append(
            next(
                event
                for event in (node._outbound.get_nowait() for _ in range(node._outbound.qsize()))
                if event.get("source_id") == POSE_SOURCE
            )
        )
    return events


def _mapper(samples: tuple[int, int]) -> LiveTagMapper:
    return LiveTagMapper(
        MapperConfig(
            session=SESSION,
            device_id=DEVICE_ID,
            camera_source_id=CAMERA_SOURCE,
            tag_source_id=TAG_SOURCE,
            camera_frame="camera",
            camera_serial="offline-ohmni-head",
            calibration_id=f"sha256:{CALIBRATION_SHA256}",
            clock_id=CLOCK_ID,
            clock_mapping_id=MAPPING_ID,
            maximum_capture_lag_ns=2_000_000,
            confidence=0.8,
            covariance_m2=(0.01, 0.0, 0.0, 0.0, 0.01, 0.0, 0.0, 0.0, 0.02),
        ),
        _Detector(),  # type: ignore[arg-type]
        receipt_time_ns=iter(
            value for sample in samples for value in (sample + 1_000_000, sample + 1_000_000)
        ).__next__,
        event_ids=iter(("camera-one", "tag-one", "camera-two", "tag-two")).__next__,
    )


def _admit(
    poses: list[dict[str, object]], samples: tuple[int, int], *, mapper: LiveTagMapper
) -> list[Observation]:
    ingress = ObservationIngress(_configuration(samples[0]), SESSION, 0)
    scope = LiveScope(SESSION, DEVICE_ID, EPOCH)
    admitted: list[Observation] = []
    for pose, sample in zip(poses, samples, strict=True):
        now = 10_000 + (sample - samples[0]) // 1_000_000
        admitted.append(
            ingress.accept(decode_submission(json.dumps(pose)), now=now, producer_role="adapter")
        )
        frame = CapturedFrame(np.zeros((480, 640, 3), dtype=np.uint8), sample)
        for event in mapper.observations(scope, frame):
            admitted.append(ingress.accept(event, now=now, producer_role="localization"))
    return admitted


def _local_request() -> dict[str, object]:
    return _request(
        {
            "schema_version": 4,
            "kind": "ohmni_tag_candidate_fusion_request",
            "candidate_mode": "local_odom",
            "source_scopes": {
                "pose": {
                    "session": SESSION,
                    "device_id": DEVICE_ID,
                    "connection_epoch": EPOCH,
                    "source_id": POSE_SOURCE,
                },
                "camera": {
                    "session": SESSION,
                    "device_id": DEVICE_ID,
                    "connection_epoch": EPOCH,
                    "source_id": CAMERA_SOURCE,
                },
                "tag": {
                    "session": SESSION,
                    "device_id": DEVICE_ID,
                    "connection_epoch": EPOCH,
                    "source_id": TAG_SOURCE,
                },
            },
            "odom_frame": "odom",
            "calibration_id": f"sha256:{CALIBRATION_SHA256}",
            "maximum_association_error_ns": 1_000,
            "maximum_translation_spread_m": 0.02,
            "maximum_rotation_spread_rad": 0.02,
            "minimum_observations_per_tag": 2,
            "observations": {"path": "observations.jsonl", "sha256": "a" * 64},
            "calibration": {"path": "calibration.json", "sha256": CALIBRATION_SHA256},
            "mount": {"path": "mount.json", "sha256": "b" * 64},
        }
    )


def _fuse(events: list[Observation]) -> dict[str, object]:
    calibration = _calibration(
        _calibration_document(), {"path": "calibration.json", "sha256": CALIBRATION_SHA256}
    )
    return fuse_observations(
        events,
        request=_local_request(),
        calibration=calibration,
        mount=_mount(_mount_document(), calibration),
        registration=None,
        input_pins={
            "observations": {"path": "observations.jsonl", "sha256": "a" * 64},
            "calibration": {"path": "calibration.json", "sha256": CALIBRATION_SHA256},
            "mount": {"path": "mount.json", "sha256": "b" * 64},
        },
    )


def test_encoder_pose_timing_is_required_for_an_offline_local_odom_tag_candidate(
    monkeypatch,
) -> None:
    first = time.monotonic_ns()
    samples = (first, first + 20_000_000)
    unqualified_device = OhmniDevice(
        Config(), shell_factory=lambda _path: _Shell(), lidar_discover=lambda: None, autostart=False
    )
    try:
        unqualified = _fuse(
            _admit(
                _pose_events(monkeypatch, unqualified_device, samples, qualified=False),
                samples,
                mapper=_mapper(samples),
            )
        )
        assert unqualified["candidate_mode"] == "local_odom"
        assert unqualified["candidate_frame"] == "odom"
        assert unqualified["candidates"] == []
        assert {item["reason"] for item in unqualified["diagnostics"]} >= {
            "missing_capture_time",
            "body_pose_not_capture_associated",
        }
    finally:
        unqualified_device.odometry.close()

    qualified_device = OhmniDevice(
        Config(), shell_factory=lambda _path: _Shell(), lidar_discover=lambda: None, autostart=False
    )
    try:
        qualified = _fuse(
            _admit(
                _pose_events(monkeypatch, qualified_device, samples, qualified=True),
                samples,
                mapper=_mapper(samples),
            )
        )
    finally:
        qualified_device.odometry.close()

    assert qualified["candidate_mode"] == "local_odom"
    assert qualified["candidate_frame"] == "odom"
    assert qualified["approval_status"] == "unapproved"
    assert len(qualified["candidates"]) == 1
    candidate = qualified["candidates"][0]
    assert candidate["tag_id"] == 7
    assert candidate["observation_count"] == 2
    assert candidate["T_odom_tag"][0][3] == 1.0
