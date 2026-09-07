from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from relay.observation_ingress import ObservationConfiguration, ObservationIngress
from relay.observations import (
    ClockMapping,
    FrameDeclaration,
    FrameRegistry,
    SourceBinding,
    decode_submission,
)

from . import device as device_module
from . import runtime as runtime_module
from .device import Config, OhmniDevice
from .fake import FakeGroundDevice
from .paired_encoder import EncoderPair
from .runtime import GroundRuntimeConfig, OhmniRuntime, main, parse_args


class _Shell:
    def close(self) -> None:
        pass


def _config() -> GroundRuntimeConfig:
    return GroundRuntimeConfig(
        relay_url="ws://127.0.0.1:1",
        session="pose-capture",
        device_id=11,
        token="offline-pose-capture-key",
        adapter_id="test-ohmni",
        pose_clock_mapping_id="qualified-test-clock",
        pose_clock_boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        maximum_pose_sample_skew_ns=20_000_000,
    )


def _publish(node: OhmniRuntime) -> list[dict[str, object]]:
    node._epoch = 1
    node._outbound = asyncio.Queue()
    node._publish_observations()
    return [node._outbound.get_nowait() for _ in range(node._outbound.qsize())]


def test_encoder_update_reaches_relay_admission_with_exact_source_time(monkeypatch) -> None:
    device = OhmniDevice(
        Config(), shell_factory=lambda _path: _Shell(), lidar_discover=lambda: None, autostart=False
    )
    sample_ns = 9_007_199_254_740_993
    receipt_ns = sample_ns + 10_000_000
    monkeypatch.setattr(runtime_module.time, "monotonic_ns", lambda: receipt_ns)
    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: receipt_ns / 1_000_000_000)
    try:
        device.odometry._update_paired_sample(
            EncoderPair(1, 100, 100, sample_ns - 3_000_000, sample_ns)
        )
        config = _config()
        frames = _publish(OhmniRuntime(config, device))
    finally:
        device.odometry.close()
    pose = next(frame for frame in frames if frame.get("source_id") == "ohmni-pose")
    submission = decode_submission(json.dumps(pose))
    assert submission.t_capture.value == sample_ns
    assert submission.t_source_receipt.value == receipt_ns
    assert submission.clock_mapping_id == config.pose_clock_mapping_id
    assert all(
        frame["t_capture"] is None and frame["clock_mapping_id"] is None
        for frame in frames
        if frame.get("type") == "observation" and frame["source_id"] != "ohmni-pose"
    )
    scope = (config.session, 11, 1, "ohmni-pose")
    mapping = ClockMapping(
        config.pose_clock_mapping_id,
        config.source_clock_id,
        "ns",
        sample_ns,
        1_000,
        1,
        1_000_000,
        1,
    )
    ingress = ObservationIngress(
        ObservationConfiguration(
            bindings=(
                SourceBinding(
                    *scope,
                    "ground",
                    ("odom", "body"),
                    ("pose",),
                    allowed_clock_mapping_ids=(mapping.mapping_id,),
                ),
            ),
            frames=FrameRegistry(
                (
                    FrameDeclaration("odom", "odom", "right_handed_z_up", "m", *scope),
                    FrameDeclaration("body", "body", "forward_left_up", "m", *scope),
                )
            ),
            clock_mappings=(mapping,),
        ),
        config.session,
        0,
    )
    accepted = ingress.accept(submission, now=1_010, producer_role="adapter")
    assert accepted.submission.t_capture.value == sample_ns


@pytest.mark.parametrize(
    ("sample_ns", "skew_ns", "quality", "configured"),
    (
        (None, None, 0.6, True),
        (1_000_000_001, 1, 0.6, True),
        (899_999_999, 1, 0.6, True),
        (999_000_000, 20_000_001, 0.6, True),
        (999_000_000, 1, 0.0, True),
        (999_000_000, 1, 0.6, False),
    ),
)
def test_unqualified_pose_timing_stays_unknown(
    monkeypatch, sample_ns, skew_ns, quality, configured
) -> None:
    device = FakeGroundDevice()
    status = replace(
        device.status(), pose_sample_ns=sample_ns, pose_sample_skew_ns=skew_ns, pos_quality=quality
    )
    monkeypatch.setattr(device, "status", lambda: status)
    monkeypatch.setattr(runtime_module.time, "monotonic_ns", lambda: 1_000_000_000)
    config = _config()
    if not configured:
        config = replace(
            config,
            pose_clock_mapping_id=None,
            pose_clock_boot_id=None,
            maximum_pose_sample_skew_ns=None,
        )
    frames = _publish(OhmniRuntime(config, device))
    pose = next(frame for frame in frames if frame.get("source_id") == "ohmni-pose")
    assert pose["t_capture"] is None
    assert pose["clock_mapping_id"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"pose_clock_mapping_id": None},
        {"pose_clock_boot_id": None},
        {"maximum_pose_sample_skew_ns": None},
        {"maximum_pose_sample_skew_ns": True},
        {"maximum_pose_sample_skew_ns": 100_000_001},
        {"pose_clock_boot_id": "old-boot"},
    ],
)
def test_pose_clock_configuration_requires_complete_bounded_values(changes) -> None:
    with pytest.raises(ValueError):
        replace(_config(), **changes)


def test_wrong_pose_clock_boot_refuses_before_device_build(monkeypatch) -> None:
    config = replace(_config(), pose_clock_boot_id="00000000-0000-0000-0000-000000000000")
    monkeypatch.setattr(runtime_module, "parse_args", lambda _argv: config)

    def forbidden_build():
        pytest.fail("device build ran before pose clock boot verification")

    monkeypatch.setattr(device_module, "build", forbidden_build)
    with pytest.raises(ValueError, match="another robot boot"):
        main([])
    with pytest.raises(ValueError, match="another robot boot"):
        OhmniRuntime(config, FakeGroundDevice())


def test_pose_capture_cli_preserves_qualified_clock_fields() -> None:
    config = _config()
    parsed = parse_args(
        [
            "--relay",
            config.relay_url,
            "--session",
            config.session,
            "--device-id",
            "11",
            "--token",
            config.token,
            "--source-clock-id",
            "qualified-robot-clock",
            "--pose-clock-mapping-id",
            config.pose_clock_mapping_id,
            "--pose-clock-boot-id",
            config.pose_clock_boot_id,
            "--maximum-pose-sample-skew-ns",
            "20000000",
        ]
    )
    assert parsed.source_clock_id == "qualified-robot-clock"
    assert parsed.pose_clock_mapping_id == config.pose_clock_mapping_id
    assert parsed.pose_clock_boot_id == config.pose_clock_boot_id
    assert parsed.maximum_pose_sample_skew_ns == 20_000_000
