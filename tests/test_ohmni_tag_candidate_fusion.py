from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from relay.observations import (
    FrameDeclaration,
    FrameRegistry,
    Observation,
    ObservationError,
    SourceBinding,
    TimingPolicy,
    decode_observation,
    ingest,
)
from tools.ohmni_tag_candidate_fusion import (
    _calibration,
    _mount,
    _registration,
    _vertical_datum,
    fuse_observations,
    run,
)
from tools.ohmni_tag_candidate_fusion import (
    _request as parse_fusion_request,
)
from tools.ohmni_world_registration import register_documents

IDENTITY = [
    [1.0, 0.0, 0.0, 0.0],
    [0.0, 1.0, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
    [0.0, 0.0, 0.0, 1.0],
]
POSE_SCOPE = {
    "session": "survey-1",
    "device_id": 9,
    "connection_epoch": 4,
    "source_id": "ohmni-pose",
}
CAMERA_SCOPE = {
    "session": "survey-1",
    "device_id": 9,
    "connection_epoch": 4,
    "source_id": "ohmni-camera",
}
TAG_SCOPE = dict(CAMERA_SCOPE)
SOURCE_SCOPES = {"pose": POSE_SCOPE, "camera": CAMERA_SCOPE, "tag": TAG_SCOPE}
CALIBRATION_ID = "sha256:" + "c" * 64


def _digest(path: Path) -> dict[str, str]:
    return {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _calibration_document() -> dict[str, object]:
    return {
        "schema_version": 2,
        "model": "fisheye",
        "status": "offline",
        "evidence_kind": "recorded_live",
        "camera_serial": "ohmni-head-1",
        "pipeline": {"resolution_px": [640, 480]},
        "checkerboard": {"inner_corners": [9, 6], "square_size_m": 0.02},
        "image_size_px": [640, 480],
        "camera_matrix": [[300.0, 0.0, 320.0], [0.0, 300.0, 240.0], [0.0, 0.0, 1.0]],
        "distortion_coefficients": [0.0, 0.0, 0.0, 0.0],
        "rms_reprojection_error_px": 0.2,
        "accepted_image_count": 20,
        "image_sha256": {f"board-{index}": f"{index:064x}" for index in range(20)},
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
        "camera_serial": "ohmni-head-1",
        "body_frame": "body",
        "camera_frame": "camera",
        "T_body_camera": IDENTITY,
    }


def _registration_document() -> dict[str, object]:
    tags = {index: [float(index), float(index % 2)] for index in range(5)}
    observed = {
        "schema_version": 1,
        "frame": "odom",
        "scope": POSE_SCOPE,
        "provenance": {"name": "actual-scan", "sha256": "a" * 64},
        "tags": [{"tag_id": identifier, "xy_m": point} for identifier, point in tags.items()],
    }
    known = {
        "schema_version": 1,
        "frame": "world",
        "map": {"map_id": "lab", "map_version": "v1", "physical_datum": "tag-0"},
        "provenance": {"name": "actual-tape", "sha256": "b" * 64},
        "tags": [{"tag_id": identifier, "xy_m": point} for identifier, point in tags.items()],
    }
    return register_documents(observed, known, held_out_tag_ids=[0, 1])


def _vertical_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "ohmni_vertical_datum_measurement",
        "measurement_kind": "scoped_odom_to_world_height",
        "source": {"frame": "odom", **POSE_SCOPE},
        "target": {
            "frame": "world",
            "map_id": "lab",
            "map_version": "v1",
            "physical_datum": "tag-0",
        },
        "z_offset_m": 0.0,
        "maximum_error_m": 0.05,
        "measured": True,
    }


def _request() -> dict[str, object]:
    return {
        "schema_version": 3,
        "kind": "ohmni_tag_candidate_fusion_request",
        "source_scopes": SOURCE_SCOPES,
        "odom_frame": "odom",
        "calibration_id": CALIBRATION_ID,
        "maximum_association_error_ns": 1_000,
        "maximum_translation_spread_m": 0.02,
        "maximum_rotation_spread_rad": 0.02,
        "minimum_observations_per_tag": 2,
        "observations": {"path": "observations.jsonl", "sha256": "0" * 64},
        "calibration": {"path": "calibration.json", "sha256": "0" * 64},
        "mount": {"path": "mount.json", "sha256": "0" * 64},
        "registration": {"path": "registration.json", "sha256": "0" * 64},
        "vertical_datum": None,
        "tape_checkpoint": {
            "schema_version": 1,
            "kind": "independent_tape_checkpoint",
            "measurement_kind": "XYZ_euclidean_distance",
            "tag_ids": [0, 1],
            "measured_distance_m": 1.0,
            "maximum_error_m": 0.05,
        },
    }


def _local_odom_request() -> dict[str, object]:
    return {
        "schema_version": 4,
        "kind": "ohmni_tag_candidate_fusion_request",
        "candidate_mode": "local_odom",
        "source_scopes": SOURCE_SCOPES,
        "odom_frame": "odom",
        "calibration_id": CALIBRATION_ID,
        "maximum_association_error_ns": 1_000,
        "maximum_translation_spread_m": 0.02,
        "maximum_rotation_spread_rad": 0.02,
        "minimum_observations_per_tag": 2,
        "observations": {"path": "observations.jsonl", "sha256": "0" * 64},
        "calibration": {"path": "calibration.json", "sha256": "0" * 64},
        "mount": {"path": "mount.json", "sha256": "0" * 64},
    }


def _event(
    event_id: str,
    frame: str,
    capture_ns: int,
    payload: dict[str, object],
    scope: dict[str, object],
    *,
    confidence: float = 0.5,
) -> Observation:
    raw = {
        "v": 1,
        "type": "observation",
        "event_id": event_id,
        **scope,
        "node_type": "ground",
        "frame": frame,
        "confidence": confidence,
        "t_capture": {"clock_id": "capture", "unit": "ns", "value": capture_ns},
        "t_source_receipt": {"clock_id": "capture", "unit": "ns", "value": capture_ns + 1},
        "clock_mapping_id": None,
        "payload": payload,
        "t_ingest": capture_ns // 1_000_000,
    }
    return decode_observation(json.dumps(raw))


def _camera(event_id: str, capture_ns: int, image_id: str) -> Observation:
    return _event(
        event_id,
        "camera",
        capture_ns,
        {
            "kind": "camera_frame",
            "image_id": image_id,
            "sha256": "d" * 64,
            "width_px": 640,
            "height_px": 480,
            "calibration_id": CALIBRATION_ID,
        },
        CAMERA_SCOPE,
    )


def _body(
    event_id: str,
    capture_ns: int,
    *,
    x_m: float = 0,
    y_m: float = 0,
    z_m: float = 0,
    qz: float = 0,
    qw: float = 1,
) -> Observation:
    return _event(
        event_id,
        "odom",
        capture_ns,
        {
            "kind": "pose",
            "pose": {
                "parent_frame": "odom",
                "child_frame": "body",
                "x_m": x_m,
                "y_m": y_m,
                "z_m": z_m,
                "qx": 0,
                "qy": 0,
                "qz": qz,
                "qw": qw,
            },
        },
        POSE_SCOPE,
    )


def _tag(event_id: str, capture_ns: int, image_id: str, tag_id: int, x_m: float) -> Observation:
    return _event(
        event_id,
        "camera",
        capture_ns,
        {
            "kind": "tag_observation",
            "family": "tag36h11",
            "tag_id": tag_id,
            "image_id": image_id,
            "pose_accepted": True,
            "tag_pose": {
                "parent_frame": "camera",
                "child_frame": f"tag:{tag_id}",
                "x_m": x_m,
                "y_m": 0,
                "z_m": 1,
                "qx": 0,
                "qy": 0,
                "qz": 0,
                "qw": 1,
            },
            "covariance_m2": [0.01, 0, 0, 0, 0.01, 0, 0, 0, 0.02],
            "reason": "pose",
            "size_m": 0.16,
            "corners_px": [[100, 100], [120, 100], [120, 120], [100, 120]],
            "pixel_frame": "rectified_camera",
            "reprojection_rms_px": 0.2,
        },
        TAG_SCOPE,
    )


def _fuse(events: list[Observation], *, vertical: bool = True) -> dict[str, object]:
    request = _request()
    calibration = _calibration(
        _calibration_document(), {"path": "calibration.json", "sha256": "c" * 64}
    )
    mount = _mount(_mount_document(), calibration)
    registration = _registration(_registration_document(), POSE_SCOPE, "odom")
    registration["vertical_offset_m"] = (
        _vertical_datum(_vertical_document(), POSE_SCOPE, "odom", registration["target"])
        if vertical
        else None
    )
    return fuse_observations(
        events,
        request=request,
        calibration=calibration,
        mount=mount,
        registration=registration,
        input_pins={
            name: {"path": f"{name}.json", "sha256": name[0] * 64}
            for name in ("observations", "calibration", "mount", "registration", "vertical_datum")
        },
    )


def _fuse_local_odom(
    events: list[Observation], mount_document: dict[str, object] | None = None
) -> dict[str, object]:
    request = parse_fusion_request(_local_odom_request())
    calibration = _calibration(
        _calibration_document(), {"path": "calibration.json", "sha256": "c" * 64}
    )
    return fuse_observations(
        events,
        request=request,
        calibration=calibration,
        mount=_mount(_mount_document() if mount_document is None else mount_document, calibration),
        registration=None,
        input_pins={
            name: {"path": f"{name}.json", "sha256": name[0] * 64}
            for name in ("observations", "calibration", "mount")
        },
    )


def test_fusion_uses_typed_canonical_observations_and_weighted_pose_chain() -> None:
    events = []
    for tag_id, x_m in ((0, 1.0), (1, 2.0)):
        for index in range(2):
            capture = 1_000_000 + tag_id * 100_000 + index * 10_000
            image = f"image-{tag_id}-{index}"
            events.extend(
                [
                    _camera(f"camera-{tag_id}-{index}", capture, image),
                    _body(f"body-{tag_id}-{index}", capture),
                    _tag(f"tag-{tag_id}-{index}", capture, image, tag_id, x_m),
                ]
            )

    result = _fuse(events)

    assert [item["tag_id"] for item in result["candidates"]] == [0, 1]
    assert result["candidates"][0]["T_world_tag"][0][3] == pytest.approx(1)
    assert result["candidates"][1]["translation_weight_sum_m2_inverse"] == pytest.approx(50)
    assert result["checkpoint"]["status"] == "evaluated"
    assert result["checkpoint"]["passes"] is True


def test_fusion_without_measured_vertical_datum_is_explicitly_odom_only() -> None:
    events = []
    for tag_id, x_m in ((0, 1.0), (1, 2.0)):
        for index in range(2):
            capture = 1_000_000 + tag_id * 100_000 + index * 10_000
            image = f"image-{tag_id}-{index}"
            events.extend(
                [
                    _camera(f"camera-{tag_id}-{index}", capture, image),
                    _body(f"body-{tag_id}-{index}", capture),
                    _tag(f"tag-{tag_id}-{index}", capture, image, tag_id, x_m),
                ]
            )

    result = _fuse(events, vertical=False)

    assert result["candidate_frame"] == "odom"
    assert "T_odom_tag" in result["candidates"][0]
    assert "T_world_tag" not in result["candidates"][0]


def test_local_odom_mode_fuses_camera_tag_through_measured_body_mount() -> None:
    mount_document = _mount_document()
    mount_document["T_body_camera"] = [
        [0.0, -1.0, 0.0, 0.5],
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.25],
        [0.0, 0.0, 0.0, 1.0],
    ]
    events = []
    for index in range(2):
        capture = 1_000_000 + index * 10_000
        image = f"local-image-{index}"
        events.extend(
            [
                _camera(f"local-camera-{index}", capture, image),
                _body(
                    f"local-body-{index}",
                    capture,
                    x_m=1.0,
                    y_m=2.0,
                    z_m=3.0,
                    qz=math.sqrt(0.5),
                    qw=math.sqrt(0.5),
                ),
                _tag(f"local-tag-{index}", capture, image, 42, 1.25),
            ]
        )

    result = _fuse_local_odom(events, mount_document)

    assert result["approval_status"] == "unapproved"
    assert result["candidate_mode"] == "local_odom"
    assert result["candidate_frame"] == "odom"
    assert result["candidates"][0]["tag_id"] == 42
    transform = result["candidates"][0]["T_odom_tag"]
    assert transform[0][3] == pytest.approx(-0.25)
    assert transform[1][3] == pytest.approx(2.5)
    assert transform[2][3] == pytest.approx(4.25)
    assert transform[0][:2] == pytest.approx([-1.0, 0.0])
    assert transform[1][:2] == pytest.approx([0.0, -1.0])
    assert {"registration", "vertical_datum", "checkpoint"}.isdisjoint(result)
    assert "world registered" in result["claim_scope"]


def test_local_odom_request_rejects_world_evidence_fields() -> None:
    request = _local_odom_request()
    request["registration"] = {"path": "registration.json", "sha256": "a" * 64}

    with pytest.raises(ValueError, match="fusion request schema is invalid"):
        parse_fusion_request(request)


def test_local_odom_mode_refuses_bad_capture_association_and_ambiguous_pose() -> None:
    bad_time = _fuse_local_odom(
        [
            _camera("late-camera-one", 1_000, "late-one"),
            _body("late-body-one", 3_000),
            _tag("late-tag-one", 1_000, "late-one", 7, 1.0),
            _camera("late-camera-two", 2_000, "late-two"),
            _body("late-body-two", 4_000),
            _tag("late-tag-two", 2_000, "late-two", 7, 1.0),
        ]
    )
    assert bad_time["candidates"] == []
    assert {
        "event_id": "late-tag-one",
        "reason": "body_pose_not_capture_associated",
    } in bad_time["diagnostics"]

    rejected_payload = dict(_tag("template", 5_000, "ambiguous", 8, 1.0).submission.payload)
    rejected_payload.update({"pose_accepted": False, "tag_pose": None, "reason": "ambiguous"})
    rejected_payload["covariance_m2"] = None
    ambiguous = _fuse_local_odom(
        [
            _camera("ambiguous-camera", 5_000, "ambiguous"),
            _body("ambiguous-body", 5_000),
            _event("ambiguous-tag", "camera", 5_000, rejected_payload, TAG_SCOPE),
        ]
    )
    assert ambiguous["candidates"] == []
    assert {"event_id": "ambiguous-tag", "reason": "tag_pose_not_accepted"} in ambiguous[
        "diagnostics"
    ]


def test_local_odom_mode_refuses_tag_spread_above_its_geometry_gate() -> None:
    events = []
    for index, x_m in enumerate((1.0, 1.1)):
        capture = 10_000 + index * 10_000
        image = f"spread-{index}"
        events.extend(
            [
                _camera(f"spread-camera-{index}", capture, image),
                _body(f"spread-body-{index}", capture),
                _tag(f"spread-tag-{index}", capture, image, 9, x_m),
            ]
        )

    result = _fuse_local_odom(events)

    assert result["candidates"] == []
    assert {"tag_id": 9, "reason": "tag translation spread exceeds the measured bound"} in result[
        "diagnostics"
    ]


def test_bad_tag_pose_covariance_and_registration_independence_are_refused() -> None:
    request = _request()
    registration = _registration(_registration_document(), POSE_SCOPE, "odom")
    registration["vertical_offset_m"] = 0.0
    request["tape_checkpoint"]["tag_ids"] = [2, 3]
    with pytest.raises(ValueError, match="independent of registration fit ties"):
        fuse_observations(
            [_camera("camera", 1, "image"), _body("body", 1), _tag("tag", 1, "image", 0, 1.0)],
            request=request,
            calibration=_calibration(
                _calibration_document(), {"path": "calibration", "sha256": "c" * 64}
            ),
            mount=_mount(_mount_document(), {"camera_serial": "ohmni-head-1"}),
            registration=registration,
            input_pins={
                name: {"path": name, "sha256": "a" * 64}
                for name in ("observations", "calibration", "mount", "registration")
            },
        )

    bad = _registration_document()
    bad["residuals"][0]["residual_m"] = 1.0
    with pytest.raises(ValueError, match="residual does not match"):
        _registration(bad, POSE_SCOPE, "odom")


def test_tag_pose_frame_and_covariance_must_be_valid_before_fusion() -> None:
    source = _tag("tag", 1, "image", 0, 1.0)
    payload = dict(source.submission.payload)
    pose = dict(payload["tag_pose"])
    pose["child_frame"] = "tag:1"
    payload["tag_pose"] = pose
    with pytest.raises(ObservationError, match="camera-to-declared-tag"):
        _event("wrong-frame", "camera", 1, payload, TAG_SCOPE)

    payload = dict(source.submission.payload)
    payload["tag_pose"] = dict(payload["tag_pose"])
    payload["covariance_m2"] = [0.01, 1.0, 0.0, 0.0, 0.01, 0.0, 0.0, 0.0, 0.02]
    with pytest.raises(ObservationError, match="symmetric positive semidefinite"):
        _event("bad-covariance", "camera", 1, payload, TAG_SCOPE)


def test_nonpositive_confidence_is_diagnostic_before_fusion() -> None:
    source = _tag("zero-confidence", 1, "image", 0, 1.0)
    result = _fuse(
        [
            _camera("camera-2", 1, "image"),
            _body("body-2", 1),
            _event(
                "zero-confidence",
                "camera",
                1,
                json.loads(json.dumps(source.submission.payload, default=dict)),
                TAG_SCOPE,
                confidence=0,
            ),
        ]
    )
    assert {"event_id": "zero-confidence", "reason": "nonpositive_confidence"} in result[
        "diagnostics"
    ]


def test_actual_camera_smoke_submissions_with_receipt_only_stay_diagnostic() -> None:
    from tools.ohmni_camera_smoke import SmokeConfig, _submission

    config = SmokeConfig(
        session=CAMERA_SCOPE["session"],
        device_id=CAMERA_SCOPE["device_id"],
        connection_epoch=CAMERA_SCOPE["connection_epoch"],
        source_id=CAMERA_SCOPE["source_id"],
        camera_serial="ohmni-head-1",
        camera_frame="camera",
        tag_sizes_m={0: 0.16},
    )
    camera_submission = _submission(
        config,
        event_id="actual-smoke-camera",
        receipt_clock_id="receipt",
        receipt_ns=100,
        payload={
            "kind": "camera_frame",
            "image_id": "actual-smoke-image",
            "sha256": "d" * 64,
            "width_px": 640,
            "height_px": 480,
            "calibration_id": CALIBRATION_ID,
        },
    )
    tag_submission = _submission(
        config,
        event_id="actual-smoke-tag",
        receipt_clock_id="receipt",
        receipt_ns=100,
        payload={
            "kind": "tag_observation",
            "family": "tag36h11",
            "tag_id": 0,
            "image_id": "actual-smoke-image",
            "pose_accepted": True,
            "tag_pose": {
                "parent_frame": "camera",
                "child_frame": "tag:0",
                "x_m": 1,
                "y_m": 0,
                "z_m": 1,
                "qx": 0,
                "qy": 0,
                "qz": 0,
                "qw": 1,
            },
            "covariance_m2": [0.01, 0, 0, 0, 0.01, 0, 0, 0, 0.02],
            "reason": "pose",
            "size_m": 0.16,
            "corners_px": [[100, 100], [120, 100], [120, 120], [100, 120]],
            "pixel_frame": "rectified_camera",
            "reprojection_rms_px": 0.2,
        },
    )
    frames = FrameRegistry(
        (
            FrameDeclaration("camera", "camera", "right_down_forward", "m", **CAMERA_SCOPE),
            FrameDeclaration("tag:0", "tag", "right_up_outward", "m", **CAMERA_SCOPE),
        )
    )
    binding = SourceBinding(
        **CAMERA_SCOPE,
        node_type="ground",
        allowed_frames=("camera", "tag:0"),
        allowed_payload_kinds=("camera_frame", "tag_observation"),
    )
    events = [
        ingest(
            submission,
            t_ingest=100,
            frames=frames,
            binding=binding,
            mappings={},
            timing=TimingPolicy(0),
        )
        for submission in (camera_submission, tag_submission)
    ]

    result = _fuse(events)

    assert result["candidates"] == []
    assert result["checkpoint"]["status"] == "not_evaluated"
    assert result["diagnostics"] == [
        {"event_id": "actual-smoke-camera", "reason": "missing_capture_time"},
        {"event_id": "actual-smoke-tag", "reason": "missing_capture_time"},
        {"reason": "checkpoint_not_evaluated"},
    ]


@pytest.mark.parametrize("missing", ("calibration", "mount"))
def test_local_odom_run_refuses_missing_measured_camera_artifacts(
    tmp_path: Path, missing: str
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    events = []
    for index in range(2):
        capture = 1_000_000 + index * 10_000
        image = f"artifact-image-{index}"
        events.extend(
            [
                _camera(f"artifact-camera-{index}", capture, image),
                _body(f"artifact-body-{index}", capture),
                _tag(f"artifact-tag-{index}", capture, image, 12, 1.0),
            ]
        )
    observations = evidence / "observations.jsonl"
    observations.write_bytes(b"".join(event.encode() + b"\n" for event in events))
    calibration = evidence / "calibration.json"
    mount = evidence / "mount.json"
    if missing != "calibration":
        calibration.write_text(json.dumps(_calibration_document()))
    if missing != "mount":
        mount.write_text(json.dumps(_mount_document()))
    request = _local_odom_request()
    request["observations"] = _digest(observations)
    request["calibration"] = (
        _digest(calibration)
        if calibration.exists()
        else {"path": calibration.name, "sha256": "a" * 64}
    )
    request["mount"] = (
        _digest(mount) if mount.exists() else {"path": mount.name, "sha256": "b" * 64}
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    output = tmp_path / "candidate.json"

    with pytest.raises(FileNotFoundError):
        run(request_path, evidence, output)

    assert not output.exists()


def test_local_odom_run_writes_an_unapproved_candidate_without_world_evidence(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    events = []
    for index in range(2):
        capture = 1_000_000 + index * 10_000
        image = f"local-run-image-{index}"
        events.extend(
            [
                _camera(f"local-run-camera-{index}", capture, image),
                _body(f"local-run-body-{index}", capture),
                _tag(f"local-run-tag-{index}", capture, image, 31, 1.5),
            ]
        )
    observations = evidence / "observations.jsonl"
    calibration = evidence / "calibration.json"
    mount = evidence / "mount.json"
    observations.write_bytes(b"".join(event.encode() + b"\n" for event in events))
    calibration.write_text(json.dumps(_calibration_document()))
    mount.write_text(json.dumps(_mount_document()))
    request = _local_odom_request()
    request["observations"] = _digest(observations)
    request["calibration"] = _digest(calibration)
    request["mount"] = _digest(mount)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    output = tmp_path / "candidate.json"

    result = run(request_path, evidence, output)

    assert output.exists()
    assert result["candidate_mode"] == "local_odom"
    assert result["candidate_frame"] == "odom"
    assert [candidate["tag_id"] for candidate in result["candidates"]] == [31]
    assert {"registration", "vertical_datum", "checkpoint"}.isdisjoint(result)


def test_run_pins_inputs_and_writes_a_bounded_create_only_candidate(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    events = []
    for tag_id, x_m in ((0, 1.0), (1, 2.0)):
        for index in range(2):
            capture = 1_000_000 + tag_id * 100_000 + index * 10_000
            image = f"image-{tag_id}-{index}"
            events.extend(
                [
                    _camera(f"camera-{tag_id}-{index}", capture, image),
                    _body(f"body-{tag_id}-{index}", capture),
                    _tag(f"tag-{tag_id}-{index}", capture, image, tag_id, x_m),
                ]
            )
    observations = evidence / "observations.jsonl"
    observations.write_bytes(b"".join(event.encode() + b"\n" for event in events))
    calibration = evidence / "calibration.json"
    mount = evidence / "mount.json"
    registration = evidence / "registration.json"
    vertical = evidence / "vertical.json"
    calibration.write_text(json.dumps(_calibration_document()))
    mount.write_text(json.dumps(_mount_document()))
    registration.write_text(json.dumps(_registration_document()))
    vertical.write_text(json.dumps(_vertical_document()))
    request = _request()
    for name, path in (
        ("observations", observations),
        ("calibration", calibration),
        ("mount", mount),
        ("registration", registration),
        ("vertical_datum", vertical),
    ):
        request[name] = _digest(path)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    output = tmp_path / "candidate.json"

    result = run(request_path, evidence, output)

    assert output.exists()
    assert result["checkpoint"]["passes"] is True
    with pytest.raises(FileExistsError):
        run(request_path, evidence, output)


def test_live_mapper_capture_events_fuse_only_as_unapproved_map_candidates() -> None:
    import numpy as np

    from perception.ohmni_pts_capture import CapturedFrame
    from relay.observations import Observation
    from tools.ohmni_live_tag_mapper import LiveScope, LiveTagMapper, MapperConfig

    class Detector:
        camera_serial = "ohmni-head-1"
        width = 640
        height = 480

        def __init__(self) -> None:
            self.calls = 0

        def detect(self, _image: np.ndarray) -> list[dict[str, object]]:
            tag_id = self.calls // 2
            self.calls += 1
            return [
                {
                    "tag_id": tag_id,
                    "pose_accepted": True,
                    "T_camera_tag": np.array(
                        [
                            [1.0, 0.0, 0.0, float(tag_id + 1)],
                            [0.0, 1.0, 0.0, 0.0],
                            [0.0, 0.0, 1.0, 1.0],
                            [0.0, 0.0, 0.0, 1.0],
                        ]
                    ),
                    "reason": "pose",
                    "size_m": 0.16,
                    "corners_px": [[100.0, 100.0], [120.0, 100.0], [120.0, 120.0], [100.0, 120.0]],
                    "pixel_frame": "rectified_camera",
                    "reprojection_rms_px": 0.2,
                }
            ]

    captures = (1_000_000, 1_010_000, 1_020_000, 1_030_000)
    receipts = iter(tuple(capture + offset for capture in captures for offset in (1, 2)))
    mapper = LiveTagMapper(
        MapperConfig(
            session=CAMERA_SCOPE["session"],
            device_id=CAMERA_SCOPE["device_id"],
            camera_source_id=CAMERA_SCOPE["source_id"],
            tag_source_id="ohmni-live-tag",
            camera_frame="camera",
            camera_serial="ohmni-head-1",
            calibration_id=CALIBRATION_ID,
            clock_id="capture",
            clock_mapping_id="live-capture",
            maximum_capture_lag_ns=5_000_000_000,
            confidence=0.8,
            covariance_m2=(0.01, 0.0, 0.0, 0.0, 0.01, 0.0, 0.0, 0.0, 0.02),
        ),
        Detector(),  # type: ignore[arg-type]
        receipt_time_ns=receipts.__next__,
        event_ids=iter(
            (
                "camera-one",
                "tag-one",
                "camera-two",
                "tag-two",
                "camera-three",
                "tag-three",
                "camera-four",
                "tag-four",
            )
        ).__next__,
    )
    scope = LiveScope(
        CAMERA_SCOPE["session"], CAMERA_SCOPE["device_id"], CAMERA_SCOPE["connection_epoch"]
    )
    mapper_events = [
        item
        for capture in captures
        for item in mapper.observations(
            scope, CapturedFrame(np.zeros((480, 640, 3), np.uint8), capture)
        )
    ]
    events = [
        *(_body(f"body-{index}", capture) for index, capture in enumerate(captures)),
        *(Observation(item, item.t_capture.value // 1_000_000) for item in mapper_events),
    ]
    request = _request()
    request["source_scopes"] = {
        **SOURCE_SCOPES,
        "tag": {**CAMERA_SCOPE, "source_id": "ohmni-live-tag"},
    }
    calibration = _calibration(
        _calibration_document(), {"path": "calibration.json", "sha256": "c" * 64}
    )
    mount = _mount(_mount_document(), calibration)
    registration = _registration(_registration_document(), POSE_SCOPE, "odom")
    registration["vertical_offset_m"] = None
    result = fuse_observations(
        events,
        request=request,
        calibration=calibration,
        mount=mount,
        registration=registration,
        input_pins={
            name: {"path": f"{name}.json", "sha256": name[0] * 64}
            for name in ("observations", "calibration", "mount", "registration", "vertical_datum")
        },
    )

    assert result["approval_status"] == "unapproved"
    assert result["candidate_frame"] == "odom"
    assert [candidate["tag_id"] for candidate in result["candidates"]] == [0, 1]
    assert all("T_odom_tag" in candidate for candidate in result["candidates"])
    assert all("T_world_tag" not in candidate for candidate in result["candidates"])
