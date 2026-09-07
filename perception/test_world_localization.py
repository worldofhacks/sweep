import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from perception.control_localization import ControlLocalizationSnapshot
from perception.world_localization import (
    MeasurementUncertainty,
    WorldEnuTransform,
    WorldLocalizationAdapter,
    WorldLocalizationError,
    WorldLocalizationPins,
)
from perception.world_localization_runtime import WorldLocalizationRuntime
from relay.observations import ClockMapping, Observation

IDENTITY = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0))
COVARIANCE = ((0.01, 0.0, 0.0), (0.0, 0.02, 0.0), (0.0, 0.0, 0.03))
_UNSET = object()


def mapping():
    return ClockMapping(
        "phone_snapshot_wall_ms", "phone_snapshot_wall_ms", "ms", 1_000_000, 1, 1, 1, 2
    )


def pins(manifest, **overrides):
    values = dict(
        drone_id=1,
        map_id=manifest["map_id"],
        map_version=manifest["bundle_version"],
        map_content_sha256=manifest["content_sha256"],
        geometry_id="geometry-v2",
        geometry_sha256="a" * 64,
        physical_datum=manifest["frame"]["physical_datum"],
        tag_source_id="laptop-detector",
        body_pose_source_id="dji-attitude",
        telemetry_source_id="dji-telemetry",
        telemetry_frame_id="dji_enu",
        height_datum_id="dji-relative-altitude-to-enu-z",
        height_alignment_measured=True,
        capture_clock_mapping_id="phone_snapshot_wall_ms",
        camera_calibration_id="mini3-delivered-720p",
        camera_calibration_sha256="b" * 64,
        camera_pipeline_id="o2-720p",
        body_extrinsics_id="dji-gimbal-attitude-capture",
        world_enu=WorldEnuTransform(
            "world-to-enu",
            (
                (0.0, -1.0, 0.0, 10.0),
                (1.0, 0.0, 0.0, 20.0),
                (0.0, 0.0, 1.0, 30.0),
                (0.0, 0.0, 0.0, 1.0),
            ),
            True,
        ),
        uncertainty=MeasurementUncertainty(
            "route-run",
            "c" * 64,
            "approved_fixture",
            "mini3-delivered-720p",
            "o2-720p",
            COVARIANCE,
            ((0.04, 0.0, 0.0), (0.0, 0.05, 0.0), (0.0, 0.0, 0.06)),
            0.07,
        ),
    )
    return WorldLocalizationPins(**(values | overrides))


def event(kind, source, frame, payload, capture=1_000_000, **overrides):
    raw = {
        "v": 1,
        "type": "observation",
        "event_id": f"{kind}-{capture}",
        "session": "live-session",
        "device_id": 1,
        "connection_epoch": 7,
        "source_id": source,
        "node_type": "aircraft",
        "frame": frame,
        "confidence": 0.9,
        "t_capture": {"clock_id": "phone_snapshot_wall_ms", "unit": "ms", "value": capture},
        "t_source_receipt": {
            "clock_id": "phone_snapshot_wall_ms",
            "unit": "ms",
            "value": capture + 1,
        },
        "clock_mapping_id": "phone_snapshot_wall_ms",
        "payload": payload,
        "t_ingest": 10,
    }
    return Observation.parse(raw | overrides)


def camera_frame(capture=1_000_000, **overrides):
    return event(
        "camera",
        "laptop-detector",
        "camera",
        {
            "kind": "camera_frame",
            "image_id": "frame-1",
            "sha256": "d" * 64,
            "width_px": 1280,
            "height_px": 720,
            "calibration_id": "mini3-delivered-720p",
        },
        capture,
        **overrides,
    )


def body_pose(capture=1_000_000, **overrides):
    return event(
        "body",
        "dji-attitude",
        "body",
        {
            "kind": "pose",
            "pose": {
                "parent_frame": "body",
                "child_frame": "camera",
                "x_m": 0.0,
                "y_m": 0.0,
                "z_m": 0.0,
                "qx": 0.0,
                "qy": 0.0,
                "qz": 0.0,
                "qw": 1.0,
            },
        },
        capture,
        **overrides,
    )


def tag(capture=1_000_000, covariance_m2=_UNSET, **overrides):
    payload = {
        "kind": "tag_observation",
        "family": "tag36h11",
        "tag_id": 0,
        "image_id": "frame-1",
        "pose_accepted": True,
        "tag_pose": {
            "parent_frame": "camera",
            "child_frame": "tag:0",
            "x_m": 0.0,
            "y_m": 0.0,
            "z_m": 2.0,
            "qx": 0.0,
            "qy": 0.0,
            "qz": 0.0,
            "qw": 1.0,
        },
        "covariance_m2": [0.01, 0.0, 0.0, 0.0, 0.01, 0.0, 0.0, 0.0, 0.01],
        "reason": "pose",
        "size_m": 0.16,
        "corners_px": [[1.0, 1.0], [2.0, 1.0], [2.0, 2.0], [1.0, 2.0]],
        "pixel_frame": "camera",
        "reprojection_rms_px": 0.2,
    }
    if covariance_m2 is not _UNSET:
        payload["covariance_m2"] = covariance_m2
    return event("tag", "laptop-detector", "camera", payload, capture, **overrides)


def telemetry(capture=2_000_000):
    return event(
        "telemetry",
        "dji-telemetry",
        "dji_enu",
        {
            "kind": "telemetry",
            "position": {"frame": "dji_enu", "x_m": 1.0, "y_m": 2.0, "z_m": 3.0},
            "velocity": {"frame": "dji_enu", "x_m_s": 0.1, "y_m_s": 0.2, "z_m_s": 0.3},
            "battery": 0.8,
            "link": 0.9,
            "pos_quality": 0.7,
            "state": "flying",
        },
        capture,
    )


@pytest.fixture
def adapter(tmp_path):
    bundle = tmp_path / "world"
    shutil.copytree(Path(__file__).parents[1] / "tests/fixtures/world_bundle", bundle)
    manifest = json.loads((bundle / "manifest.yaml").read_text())
    return WorldLocalizationAdapter(
        bundle,
        {manifest["bundle_version"]: manifest["content_sha256"]},
        pins(manifest),
        mapping(),
        allow_fixture_evidence=True,
    )


def test_camera_tag_and_dynamic_capture_pose_become_rotated_enu_fix(adapter):
    assert adapter.ingest(camera_frame(), connection_epoch=7) == ()
    assert adapter.ingest(body_pose(), connection_epoch=7) == ()
    (fix,) = adapter.ingest(tag(), connection_epoch=7)
    np.testing.assert_allclose(fix.position_map_enu_m, (-20.0, 10.0, -32.0))
    np.testing.assert_allclose(
        fix.covariance_map_enu_m2, ((0.02, 0, 0), (0, 0.01, 0), (0, 0, 0.03))
    )
    assert fix.capture_time == pytest.approx(0.001)
    assert fix.extrinsics.capture_time == fix.capture_time


def test_tag_requires_same_capture_time_body_pose_and_camera_frame(adapter):
    adapter.ingest(camera_frame(), connection_epoch=7)
    adapter.ingest(body_pose(capture=999_999), connection_epoch=7)
    with pytest.raises(WorldLocalizationError, match="capture-time body extrinsics"):
        adapter.ingest(tag(), connection_epoch=7)


def test_dynamic_capture_pose_rotation_enters_the_world_body_transform(adapter):
    adapter.ingest(camera_frame(), connection_epoch=7)
    adapter.ingest(
        event(
            "body",
            "dji-attitude",
            "body",
            {
                "kind": "pose",
                "pose": {
                    "parent_frame": "body",
                    "child_frame": "camera",
                    "x_m": 1.0,
                    "y_m": 0.0,
                    "z_m": 0.0,
                    "qx": 0.0,
                    "qy": 0.0,
                    "qz": 2**-0.5,
                    "qw": 2**-0.5,
                },
            },
        ),
        connection_epoch=7,
    )
    (fix,) = adapter.ingest(tag(), connection_epoch=7)
    np.testing.assert_allclose(fix.position_map_enu_m, (-19.0, 10.0, -32.0))


def test_local_enu_telemetry_is_preserved_and_height_is_explicit(adapter):
    velocity, height = adapter.ingest(telemetry(), connection_epoch=7)
    assert velocity.velocity_map_enu_mps == pytest.approx((0.1, 0.2, 0.3))
    assert height.height_map_enu_m == pytest.approx(3.0)
    assert height.variance_m2 == pytest.approx(0.07)


def test_unpinned_frame_map_or_epoch_is_refused(adapter):
    with pytest.raises(WorldLocalizationError, match="current aircraft epoch"):
        adapter.ingest(camera_frame(connection_epoch=8), connection_epoch=7)
    with pytest.raises(WorldLocalizationError, match="source is unpinned"):
        adapter.ingest(camera_frame(source_id="other"), connection_epoch=7)
    with pytest.raises(WorldLocalizationError, match="clock mapping is unpinned"):
        adapter.ingest(camera_frame(clock_mapping_id="other"), connection_epoch=7)


def test_runtime_drains_the_localization_socket_and_enqueues_only_canonical_measurements(adapter):
    class Publisher:
        config = SimpleNamespace(mode="live", drones={1: object()})

        def __init__(self):
            self.enqueued = []
            self.refused = []

        def take_live_observations(self, drone_id):
            assert drone_id == 1
            return SimpleNamespace(connection_epoch=7), tuple(
                item.to_mapping() for item in (camera_frame(), body_pose(), tag(), telemetry())
            )

        def enqueue(self, raw):
            self.enqueued.append(raw)

        def refuse_input(self, raw, reason):
            self.refused.append((raw, reason))

        def publish_live(self, drone_id, monotonic_s):
            return {"type": "control_localization", "drone_id": drone_id, "t": monotonic_s}

    publisher = Publisher()
    result = WorldLocalizationRuntime(publisher, {1: adapter}).tick(1, 2.0)
    assert result["type"] == "control_localization"
    assert [item["kind"] for item in publisher.enqueued] == ["tag", "velocity", "height"]
    assert publisher.refused == []


def test_host_pinned_uncertainty_allows_nullable_detector_covariance(adapter):
    adapter.ingest(camera_frame(), connection_epoch=7)
    adapter.ingest(body_pose(), connection_epoch=7)
    (fix,) = adapter.ingest(tag(covariance_m2=None), connection_epoch=7)
    np.testing.assert_allclose(
        fix.covariance_map_enu_m2, ((0.02, 0, 0), (0, 0.01, 0), (0, 0, 0.03))
    )


def test_capture_time_is_required_before_any_camera_measurement(adapter):
    with pytest.raises(WorldLocalizationError, match="requires a capture timestamp"):
        adapter.ingest(camera_frame(t_capture=None), connection_epoch=7)


def test_dynamic_body_pose_has_its_own_pinned_source(adapter):
    with pytest.raises(WorldLocalizationError, match="body-camera source is unpinned"):
        adapter.ingest(body_pose(source_id="laptop-detector"), connection_epoch=7)


def test_control_snapshot_is_projected_back_to_the_pinned_world_frame(adapter):
    snapshot = ControlLocalizationSnapshot(
        drone_id=1,
        connection_epoch=7,
        map_id=adapter.pins.map_id,
        geometry_id=adapter.pins.geometry_id,
        capture_clock_id=adapter.pins.capture_clock_mapping_id,
        evaluated_at_s=1.0,
        position_map_enu_m=(-20.0, 10.0, -32.0),
        velocity_map_enu_mps=None,
        covariance_map_enu_m2=((0.02, 0, 0), (0, 0.01, 0), (0, 0, 0.03)),
        fix_age_s=0.0,
        velocity_age_s=None,
        height_age_s=None,
        confidence="green",
        loss_age_s=None,
        status="ready",
        control_eligible=True,
        reason="ready",
        last_rejection=None,
        active_contradictions=(),
        source_ids=(adapter.pins.tag_source_id,),
        camera_calibration_id=adapter.pins.camera_calibration_id,
        body_extrinsics_id=adapter.pins.body_extrinsics_id,
        retained_event_count=1,
    )

    projected = adapter.project_world(snapshot)

    assert projected is not None
    assert projected.position_world_m == pytest.approx((0.0, 0.0, -2.0))
    np.testing.assert_allclose(
        projected.covariance_world_m2, ((0.01, 0, 0), (0, 0.02, 0), (0, 0, 0.03))
    )
