from __future__ import annotations

import time

from .device import Config, OhmniDevice
from .odometry import MAX_SAMPLE_GAP_S, Odometry
from .paired_encoder import EncoderPair


class _Shell:
    def close(self) -> None:
        pass


def test_device_status_preserves_exact_paired_encoder_timing() -> None:
    device = OhmniDevice(
        Config(),
        shell_factory=lambda _path: _Shell(),
        lidar_discover=lambda: None,
        autostart=False,
    )
    right_receipt_ns = time.monotonic_ns() - 5_000_000
    left_receipt_ns = right_receipt_ns - 3_000_000
    try:
        device.odometry._update_paired_sample(
            EncoderPair(1, 100, 100, left_receipt_ns, right_receipt_ns)
        )

        status = device.status()

        assert status.pos_quality == 0.6
        assert status.pose_sample_ns == right_receipt_ns
        assert status.pose_sample_skew_ns == 3_000_000
    finally:
        device.odometry.close()


def test_paired_snapshot_keeps_its_timestamp_and_skew_together() -> None:
    odometry = Odometry(object(), (0.0, 0.0, 0.0))
    odometry._update_paired_sample(EncoderPair(1, 100, 100, 900_000_000, 1_000_000_000))
    odometry._update_paired_sample(EncoderPair(2, 101, 101, 1_030_000_000, 1_100_000_000))

    pose = odometry.snapshot(1.1)

    assert pose.sample_ns == 1_100_000_000
    assert pose.sample_skew_ns == 70_000_000


def test_stale_or_lost_pose_clears_paired_timing() -> None:
    odometry = Odometry(object(), (0.0, 0.0, 0.0))
    odometry._update_paired_sample(EncoderPair(1, 100, 100, 900_000_000, 1_000_000_000))

    stale = odometry.snapshot(1.0 + MAX_SAMPLE_GAP_S + 0.001)

    assert stale.quality == 0.0
    assert stale.sample_ns is None
    assert stale.sample_skew_ns is None

    odometry._update_paired_sample(EncoderPair(2, 101, 101, 1_350_000_000, 1_400_000_000))
    lost = odometry.snapshot(1.4)

    assert lost.quality == 0.0
    assert lost.sample_ns is None
    assert lost.sample_skew_ns is None


def test_legacy_odometry_update_leaves_timing_unknown() -> None:
    odometry = Odometry(object(), (0.0, 0.0, 0.0))

    odometry.update((100, 100), 1.0)

    pose = odometry.snapshot(1.0)
    assert pose.quality == 0.6
    assert pose.sample_ns is None
    assert pose.sample_skew_ns is None
